"""
lifecycle.py had no tests, and it is the thousand lines that touch the box.

Every subprocess here is a fake `subprocess.run` recorded on a list, so the
tests read like the incidents they pin:

  - a restart whose stop failed must not start on top of the old process;
  - a slow start script must not freeze the event loop (Lodestar's 2s probe
    used to declare the node dead mid-restart);
  - two concurrent starts of one service run one script, not two;
  - reclaim leaves the constellation and the observatory itself alone
    unless told otherwise, and says why;
  - a manifest dropped in after boot is controllable on the next request;
  - a start that ran and failed is a 200 with the stderr, not a 500;
  - an update package may not land outside the home directory.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seren_observatory import lifecycle, manifests
from seren_observatory.app import create_app
from seren_observatory.config import ObservatoryConfig
from seren_observatory.system_routes import reclaim_plan


class FakeRun:
    """Stands in for subprocess.run. `script` decides the outcome per argv."""

    def __init__(self, *, rc=0, stdout="", stderr="", delay=0.0, on_call=None):
        self.rc, self.stdout, self.stderr, self.delay, self.on_call = rc, stdout, stderr, delay, on_call
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if self.delay:
            time.sleep(self.delay)
        if self.on_call:
            self.on_call(argv)
        return subprocess.CompletedProcess(argv, self.rc, stdout=self.stdout, stderr=self.stderr)


def _manifest(fake_home: Path, name="llama", port=8080, **extra) -> dict:
    d = fake_home / ".seren" / "services"
    d.mkdir(parents=True, exist_ok=True)
    m = {"schema_version": 2, "service": name, "service_type": "pid_file", "port": port,
         "start_script": str(fake_home / f"start_{name}.sh"),
         "stop_script": str(fake_home / f"stop_{name}.sh"),
         "pid_path": str(fake_home / f"{name}.pid"), **extra}
    (fake_home / f"start_{name}.sh").write_text("#!/bin/bash\n")
    (fake_home / f"stop_{name}.sh").write_text("#!/bin/bash\n")
    (d / f"{name}.json").write_text(json.dumps(m))
    return m


def _alive_pid(fake_home: Path, name="llama"):
    (fake_home / f"{name}.pid").write_text(str(os.getpid()))


def _no_pid(fake_home: Path, name="llama"):
    p = fake_home / f"{name}.pid"
    if p.exists():
        p.unlink()


# ── restart honesty ──────────────────────────────────────────────────────

def test_a_failed_stop_does_not_start_on_top_of_the_old_process(fake_home, monkeypatch):
    m = _manifest(fake_home)
    _alive_pid(fake_home)
    run = FakeRun(rc=1, stderr="stop: permission denied")
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    res = lifecycle._restart_sync(m)
    assert res["ok"] is False
    assert "stop failed" in res["error"]
    assert res["start"] is None
    assert all(argv[1].endswith("stop_llama.sh") for argv in run.calls), "start script must not have run"


def test_a_stop_that_exits_zero_but_leaves_the_process_alive_is_an_error(fake_home, monkeypatch):
    m = _manifest(fake_home)
    _alive_pid(fake_home)                      # and the fake stop leaves it there
    run = FakeRun(rc=0)
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    res = lifecycle._restart_sync(m)
    assert res["ok"] is False
    assert "still alive" in res["error"]
    assert res["start"] is None


def test_a_clean_restart_stops_then_starts(fake_home, monkeypatch):
    m = _manifest(fake_home, port=0)           # port 0: no port-release wait
    _alive_pid(fake_home)

    def on_call(argv):
        if argv[1].endswith("stop_llama.sh"):
            _no_pid(fake_home)
        else:
            _alive_pid(fake_home)
    run = FakeRun(on_call=on_call)
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    res = lifecycle._restart_sync(m)
    assert res["ok"] is True
    assert [Path(a[1]).name for a in run.calls] == ["stop_llama.sh", "start_llama.sh"]


# ── the event loop stays free ────────────────────────────────────────────

async def test_a_slow_start_script_does_not_block_the_loop(fake_home, monkeypatch):
    m = _manifest(fake_home)
    run = FakeRun(delay=0.4)
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    ticks = []

    async def heartbeat():
        for _ in range(4):
            await asyncio.sleep(0.05)
            ticks.append(time.monotonic())

    t0 = time.monotonic()
    _, res = await asyncio.gather(heartbeat(), lifecycle.start(m))
    assert res["ok"] is True
    assert len(ticks) == 4
    assert ticks[-1] - t0 < 0.35, "the heartbeat should finish while the script is still running"


async def test_two_concurrent_starts_run_one_script(fake_home, monkeypatch):
    m = _manifest(fake_home)

    def on_call(argv):
        _alive_pid(fake_home)                  # the script "started" it
    run = FakeRun(delay=0.1, on_call=on_call)
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    a, b = await asyncio.gather(lifecycle.start(m), lifecycle.start(m))
    assert len(run.calls) == 1, "the per-service lock serialises; the second sees already_running"
    assert {a.get("already_running"), b.get("already_running")} == {None, True}


async def test_locks_are_per_service_not_global(fake_home, monkeypatch):
    llama = _manifest(fake_home, "llama")
    kokoro = _manifest(fake_home, "kokoro", port=8880)
    run = FakeRun(delay=0.2)
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    t0 = time.monotonic()
    await asyncio.gather(lifecycle.start(llama), lifecycle.start(kokoro))
    assert time.monotonic() - t0 < 0.35, "two different services start in parallel"


# ── reclaim policy ───────────────────────────────────────────────────────

def _fleet():
    return {
        "llama": {"service": "llama", "service_type": "pid_file", "port": 8090},
        "kokoro": {"service": "kokoro", "service_type": "pid_file", "port": 8880},
        "coral": {"service": "coral", "service_type": "library", "port": 0},
        "seren-lodestar": {"service": "seren-lodestar", "service_type": "systemd",
                           "systemd_unit": "seren-lodestar", "port": 6361},
        "searxng": {"service": "searxng", "service_type": "docker_compose",
                    "compose_file": "/x/compose.yml", "port": 8888},
        "observatory": {"service": "observatory", "service_type": "systemd",
                        "systemd_unit": "seren-observatory", "port": 7777},
    }


def test_reclaim_by_default_takes_only_the_gpu_daemons():
    cands, kept = reclaim_plan(_fleet(), exclude=set(), include=set(), everything=False)
    assert sorted(n for n, _ in cands) == ["kokoro", "llama"]
    why = {k["service"]: k["why"] for k in kept}
    assert "never stops itself" in why["observatory"]
    assert "not a GPU daemon" in why["seren-lodestar"]
    assert "not a GPU daemon" in why["searxng"]
    assert "library" in why["coral"]


def test_reclaim_include_names_one_infrastructure_service():
    cands, _ = reclaim_plan(_fleet(), exclude=set(), include={"searxng"}, everything=False)
    assert sorted(n for n, _ in cands) == ["kokoro", "llama", "searxng"]


def test_reclaim_all_still_never_stops_the_observatory_and_honours_exclude():
    cands, kept = reclaim_plan(_fleet(), exclude={"llama"}, include=set(), everything=True)
    names = sorted(n for n, _ in cands)
    assert "observatory" not in names
    assert "llama" not in names
    assert "seren-lodestar" in names and "searxng" in names


# ── routes: discovered per request, honest on failure ────────────────────

@pytest.fixture
def client(fake_home, monkeypatch):
    monkeypatch.setattr("seren_observatory.auth.load_token", lambda: "tok")
    monkeypatch.setattr("seren_observatory.app.load_token", lambda: "tok")
    app = create_app(ObservatoryConfig())
    return TestClient(app, headers={"Authorization": "Bearer tok"})


def test_a_manifest_dropped_in_after_boot_is_controllable_without_a_restart(client, fake_home, monkeypatch):
    assert client.post("/api/v1/service/kokoro/start").status_code == 404
    _manifest(fake_home, "kokoro", port=8880)
    run = FakeRun(on_call=lambda argv: _alive_pid(fake_home, "kokoro"))
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    r = client.post("/api/v1/service/kokoro/start")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert client.get("/api/v1/service/kokoro/manifest").status_code == 200


def test_a_start_that_ran_and_failed_is_200_with_the_stderr(client, fake_home, monkeypatch):
    _manifest(fake_home)
    run = FakeRun(rc=3, stderr="CUDA out of memory")
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    r = client.post("/api/v1/service/llama/start")
    assert r.status_code == 200, "a failure the script reported is an answer, not an outage"
    body = r.json()
    assert body["ok"] is False and body["exit_code"] == 3
    assert "CUDA out of memory" in body["stderr"]


def test_specific_handlers_are_mounted_and_404_when_not_installed(client):
    r = client.get("/api/v1/service/llama/models")
    assert r.status_code == 404
    assert "not installed" in r.json()["detail"]


def test_update_refuses_a_destination_outside_home(client, fake_home, tmp_path, monkeypatch):
    monkeypatch.delenv("SEREN_AGENT_UPDATE_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(fake_home), 1) if p.startswith("~") else p)
    outside = str(tmp_path.parent / "elsewhere")
    r = client.post("/api/v1/system/observatory-update",
                    files={"package": ("x.tar.gz", b"data", "application/octet-stream")},
                    data={"dest_path": outside})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "outside" in body["error"]
    assert not Path(outside).exists()


def test_reboot_with_a_junk_delay_is_an_answer_not_a_500(client, monkeypatch):
    r = client.post("/api/v1/system/reboot", json={"delay_minutes": "soon"})
    assert r.status_code == 200
    assert r.json()["scheduled"] is False and "whole number" in r.json()["error"]


def test_docs_are_public_but_the_api_is_not(fake_home, monkeypatch):
    monkeypatch.setattr("seren_observatory.app.load_token", lambda: "tok")
    c = TestClient(create_app(ObservatoryConfig()))
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/api/v1/system/services").status_code == 401
