"""
The receiving end of a ripple (28 Sept 2026): a hippocampus on another node -
the Nano - asks the model that lives on THIS box, and the Observatory starts
the configured command as the person.

- off until ripple.enabled: an unset node answers 409 and runs nothing
- the caller sends the message, never the command: the node's own command runs
  with {message} filled in one argument and the event in SEREN_RIPPLE_*
- no bearer, no ripple (it is a POST on this plane)
- one at a time per event; an empty or oversized message is refused
- a root / LocalSystem Observatory with no run_as refuses (seren_sinew.runas)
- the yaml ripple: block is read like updates:, bad values fall back with a note
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seren_observatory.app import create_app
from seren_observatory.config import ObservatoryConfig, RippleConfig, load_config

RECORDER = """
import json, os, sys, time
out = sys.argv[1]
if len(sys.argv) > 3 and sys.argv[3] == "slow":
    time.sleep(3)
with open(out, "w", encoding="utf-8") as f:
    json.dump({"argv": sys.argv[2:], "event": os.environ.get("SEREN_RIPPLE_EVENT")}, f)
"""


@pytest.fixture
def recorder(tmp_path):
    rec = tmp_path / "recorder.py"
    rec.write_text(RECORDER, encoding="utf-8")
    return rec, tmp_path / "rippled.json"


def _client(fake_home, monkeypatch, ripple: RippleConfig, token="tok"):
    monkeypatch.setattr("seren_observatory.auth.load_token", lambda *a, **k: token)
    monkeypatch.setattr("seren_observatory.app.load_token", lambda *a, **k: token)
    app = create_app(ObservatoryConfig(ripple=ripple))
    app.state.ripple._log_dir = fake_home / "seren-logs"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return app, TestClient(app, headers=headers)


def _wait_for(path: Path, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        if path.exists() and path.stat().st_size:
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(0.1)
    raise AssertionError(f"the ripple never wrote {path}")


def test_off_until_enabled(fake_home, monkeypatch):
    _, c = _client(fake_home, monkeypatch, RippleConfig())
    r = c.post("/api/v1/system/ripple", json={"event": "brief_requested", "message": "bedtime"})
    assert r.status_code == 409 and "not set up" in r.json()["error"]


def test_the_nodes_own_command_runs_with_the_callers_message(fake_home, monkeypatch, recorder):
    rec, out = recorder
    app, c = _client(fake_home, monkeypatch, RippleConfig(
        enabled=True, command=[sys.executable, str(rec), str(out), "{message}", "{draft_id}"]))
    r = c.post("/api/v1/system/ripple", json={"event": "draft_submitted", "message": "review draft d1",
                                              "draft_id": "d1", "command": ["rm", "-rf", "/"]})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    got = _wait_for(out)
    assert got["argv"] == ["review draft d1", "d1"], "the caller's 'command' key is ignored"
    assert got["event"] == "draft_submitted"
    app.state.ripple.wait()


def test_no_bearer_no_ripple(fake_home, monkeypatch, recorder):
    rec, out = recorder
    app, _ = _client(fake_home, monkeypatch, RippleConfig(enabled=True, command=[sys.executable, str(rec), str(out)]))
    r = TestClient(app).post("/api/v1/system/ripple", json={"message": "hi"})
    assert r.status_code == 401
    assert not out.exists()


def test_one_ripple_at_a_time_per_event(fake_home, monkeypatch, recorder):
    rec, out = recorder
    app, c = _client(fake_home, monkeypatch, RippleConfig(
        enabled=True, command=[sys.executable, str(rec), str(out), "{message}", "slow"]))
    first = c.post("/api/v1/system/ripple", json={"event": "brief_requested", "message": "bedtime"}).json()
    second = c.post("/api/v1/system/ripple", json={"event": "brief_requested", "message": "bedtime"}).json()
    assert first["ok"] is True
    assert second["ok"] is False and "still running" in second["skipped"]
    app.state.ripple.wait()


@pytest.mark.parametrize("message,status", [("", 400), ("x" * 8001, 413)])
def test_an_empty_or_oversized_message_is_refused(fake_home, monkeypatch, recorder, message, status):
    rec, out = recorder
    _, c = _client(fake_home, monkeypatch, RippleConfig(enabled=True, command=[sys.executable, str(rec), str(out)]))
    assert c.post("/api/v1/system/ripple", json={"message": message}).status_code == status


def test_a_privileged_observatory_with_no_run_as_refuses(fake_home, monkeypatch, recorder):
    from seren_sinew import runas
    monkeypatch.setattr(runas, "whoami", lambda: ("SYSTEM", True))
    rec, out = recorder
    _, c = _client(fake_home, monkeypatch, RippleConfig(enabled=True, command=[sys.executable, str(rec), str(out)]))
    r = c.post("/api/v1/system/ripple", json={"message": "bedtime"})
    assert r.status_code == 409 and "run_as is empty" in r.json()["error"]
    assert not out.exists()


def test_the_yaml_ripple_block_is_read(tmp_path, monkeypatch, capsys):
    for k in ("AGENT_HOST", "AGENT_PORT", "SEREN_AGENT_HOST", "SEREN_AGENT_PORT"):
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / "seren-observatory.yaml"
    p.write_text("ripple:\n  enabled: true\n  run_as: alice\n  command: 'claude -p \"{message}\"'\n"
                 "  timeout_seconds: soon\n")
    cfg = load_config(str(p))
    assert cfg.ripple.enabled is True and cfg.ripple.run_as == "alice"
    assert cfg.ripple.command == 'claude -p "{message}"'
    assert cfg.ripple.timeout_seconds == 900
    assert "ignored bad value for 'ripple.timeout_seconds'" in capsys.readouterr().out
