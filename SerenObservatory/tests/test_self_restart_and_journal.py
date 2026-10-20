"""
Chad's third pass, 20 Oct 2026, the two bugs:

- Windows: "observatory cannot restart itself, though nssm it seems to throw
  a stop then start" - sc stop from inside the request killed the answer
  (WinError 10054). A self-restart now answers first, then leaves, and NSSM
  starts it again, with the self-upgrade's checks; a check that fails on an
  Observatory manifest is an answer, not a kill; any other Windows service
  still goes through sc.
- Linux: "no logs for any of the seren services are appearing" - a systemd
  unit with no log_path logs to journald, so its tail is journalctl's.
"""
from __future__ import annotations

import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from seren_observatory import lifecycle, upgrades
from seren_observatory.app import create_app


@pytest.fixture
def win(fake_home, monkeypatch):
    svc = fake_home / ".seren" / "services"
    (svc / "seren-observatory-wren.json").write_text(json.dumps({
        "schema_version": 2, "service": "seren-observatory-wren", "service_type": "windows_service",
        "windows_service": "SerenObservatory-wren", "port": 7251}))
    (svc / "seren-symposium-wren.json").write_text(json.dumps({
        "schema_version": 2, "service": "seren-symposium-wren", "service_type": "windows_service",
        "windows_service": "SerenSymposium-wren", "port": 7262}))
    monkeypatch.setattr("seren_observatory.auth.load_token", lambda *a, **k: "tok")
    monkeypatch.setattr("seren_observatory.app.load_token", lambda *a, **k: "tok")
    left = []
    monkeypatch.setattr(upgrades, "_exit_process", lambda: left.append(time.monotonic()))
    monkeypatch.setattr(upgrades, "EXIT_GRACE_S", 0.01)
    restarted = []
    monkeypatch.setattr(lifecycle, "_restart_sync", lambda m: (restarted.append(m["service"]), {"ok": True, "stop": {}, "start": {}})[1])
    return TestClient(create_app(), headers={"Authorization": "Bearer tok"}), left, restarted


def test_a_self_restart_answers_then_hands_itself_to_nssm(win, monkeypatch):
    c, left, restarted = win
    monkeypatch.setattr(upgrades, "nssm_handoff_check",
                        lambda m: {"ok": True, "service": "SerenObservatory-wren", "service_pid": 7,
                                   "exit_policy": {"policy": "Restart", "source": "registry"}})
    with c:
        r = c.post("/api/v1/service/seren-observatory-wren/restart")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["handed_to"] == "nssm" and body["service"] == "SerenObservatory-wren"
        for _ in range(50):
            if left:
                break
            time.sleep(0.02)
        assert left, "the exit is scheduled after the answer, not before"
    assert restarted == [], "sc stop/start never ran on itself"


def test_a_self_restart_nssm_would_not_bring_back_is_refused_and_kills_nothing(win, monkeypatch):
    c, left, restarted = win
    monkeypatch.setattr(upgrades, "nssm_handoff_check",
                        lambda m: {"ok": False, "error": "SerenObservatory-wren's exit policy is Exit, not Restart"})
    with c:
        body = c.post("/api/v1/service/seren-observatory-wren/restart").json()
        assert body["ok"] is False and "exit policy is Exit" in body["error"] and body["handed_to"] is None
        time.sleep(0.05)
    assert left == [] and restarted == []


def test_another_windows_service_still_restarts_through_sc(win, monkeypatch):
    c, left, restarted = win
    monkeypatch.setattr(upgrades, "nssm_handoff_check",
                        lambda m: {"ok": False, "error": "this process is not SerenSymposium-wren's program"})
    with c:
        body = c.post("/api/v1/service/seren-symposium-wren/restart").json()
        assert body["ok"] is True
        time.sleep(0.05)
    assert restarted == ["seren-symposium-wren"] and left == []


def test_a_systemd_unit_without_a_log_path_tails_the_journal(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=(
            "-- Logs begin at Fri 2026-10-16 17:49:32 CDT, end at Tue 2026-10-20 15:23:53 CDT. --\n"
            "2026-10-20T15:23:52-0500 nuc python[3785812]: INFO: GET /health 200\n"
            "2026-10-20T15:23:53-0500 nuc python[3785812]: INFO: GET /health 200\n"), stderr="")
    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)
    out = lifecycle._tail_log_sync({"service": "seren-memory-wren", "service_type": "systemd",
                                    "systemd_unit": "seren-memory-wren.service", "port": 7250}, lines=50)
    assert out["ok"] is True and len(out["lines"]) == 2 and out["lines"][0].startswith("2026-10-20T15:23:52")
    assert calls == [["journalctl", "-u", "seren-memory-wren.service", "-n", "50", "--no-pager", "-o", "short-iso"]]
    assert out["log_path"] == "journalctl -u seren-memory-wren.service"


def test_a_systemd_unit_with_a_log_path_still_tails_the_file(tmp_path, monkeypatch):
    log = tmp_path / "x.log"
    log.write_text("one\ntwo\n", encoding="utf-8")
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *a, **k: pytest.fail("journalctl must not run"))
    out = lifecycle._tail_log_sync({"service": "x", "service_type": "systemd", "systemd_unit": "x.service",
                                    "log_path": str(log)}, lines=10)
    assert out["ok"] is True and out["lines"] == ["one", "two"]


def test_a_journal_this_account_cannot_read_says_so(monkeypatch):
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, stdout="", stderr="No journal files were found.\nHint: You are currently not seeing messages from other users and the system.\n      Users in groups 'adm', 'systemd-journal' can see all messages. Permission denied."))
    out = lifecycle._tail_log_sync({"service": "y", "service_type": "systemd", "systemd_unit": "y.service"}, lines=10)
    assert out["ok"] is False and "adm" in out["note"]
