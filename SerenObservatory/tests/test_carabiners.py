"""
The carabiners on this box, for Lodestar's Rack (22 Oct 2026): the
Observatory runs the .kbh as the person and passes on what it answered.

- off until carabiners.enabled, and shut even to a GET without a token
- a caller names a carabiner; only a .kbh in the configured folder runs
- the change reaches kbh in its environment; the answer comes back whole
- the carabiner's refusals reach the caller as the carabiner said them
"""
from __future__ import annotations

import json
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from seren_observatory.app import create_app
from seren_observatory.config import CarabinersConfig, ObservatoryConfig, load_config

FAKE_KBH = textwrap.dedent('''
    import json, os, sys
    args = sys.argv[1:]
    name, rest = args[0], args[1:]
    if rest[:2] == ["config", "show"]:
        print(json.dumps({"ok": True, "carabiner": name, "display": "Claude Code", "version": "5.3.0",
                          "fields": [{"key": "session.model", "kind": "choice", "value": ""}], "connections": []}))
    elif rest[:2] == ["config", "apply"]:
        req = json.loads(os.environ["KBH_SETTINGS_REQUEST"])
        bad = {k: "not one of: manual, plan" for k, v in req["values"].items() if v == "yolo"}
        print(json.dumps({"ok": not bad, "carabiner": name, "applied": [] if bad else list(req["values"]),
                          "refused": bad, "applies": {}, "belay_ok": None if bad else True, "belay": [],
                          "error": "nothing was written" if bad else ""}))
    elif rest[:1] == ["belay"]:
        print(json.dumps({"carabiner": name, "ok": True, "checks": [{"verb": "wake", "status": "held"}]}))
''')


@pytest.fixture
def kbh_folder(tmp_path):
    folder = tmp_path / "kbh"
    folder.mkdir()
    (folder / "claude.kbh").write_text(FAKE_KBH, encoding="utf-8")
    (folder / "claude.yaml").write_text("carabiner: claude\n", encoding="utf-8")
    return folder


def _client(fake_home, monkeypatch, carabiners: CarabinersConfig, token="tok"):
    monkeypatch.setattr("seren_observatory.auth.load_token", lambda *a, **k: token)
    monkeypatch.setattr("seren_observatory.app.load_token", lambda *a, **k: token)
    app = create_app(ObservatoryConfig(carabiners=carabiners))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, headers=headers)


def _on(folder) -> CarabinersConfig:
    return CarabinersConfig(enabled=True, folder=str(folder), python=sys.executable)


def test_off_until_enabled(fake_home, monkeypatch, kbh_folder):
    c = _client(fake_home, monkeypatch, CarabinersConfig(folder=str(kbh_folder)))
    r = c.get("/api/v1/carabiners")
    assert r.status_code == 409 and "carabiners.enabled" in r.json()["error"]


def test_shut_without_a_token_even_for_a_get(fake_home, monkeypatch, kbh_folder):
    c = _client(fake_home, monkeypatch, _on(kbh_folder), token=None)
    assert c.get("/api/v1/carabiner/claude/settings").status_code == 403


def test_no_bearer_no_change(fake_home, monkeypatch, kbh_folder):
    app_client = _client(fake_home, monkeypatch, _on(kbh_folder))
    r = TestClient(app_client.app).post("/api/v1/carabiner/claude/settings", json={"values": {"x": 1}})
    assert r.status_code == 401


def test_the_rack_reads_lists_changes_and_belays(fake_home, monkeypatch, kbh_folder, node_manifest):
    c = _client(fake_home, monkeypatch, _on(kbh_folder))
    listed = c.get("/api/v1/carabiners").json()
    assert listed["ok"] and listed["node"] == "test-jetson"
    assert [e["carabiner"] for e in listed["carabiners"]] == ["claude"] and listed["carabiners"][0]["configured"]

    m = c.get("/api/v1/carabiner/claude/settings").json()
    assert m["ok"] and m["version"] == "5.3.0" and m["node"] == "test-jetson" and m["fields"][0]["key"] == "session.model"

    r = c.post("/api/v1/carabiner/claude/settings",
               json={"node": "test-jetson", "values": {"session.model": "claude-haiku-5-5"}, "holder": "lodestar-rack"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["applied"] == ["session.model"], r.text

    r = c.post("/api/v1/carabiner/claude/settings", json={"values": {"session.permission_mode": "yolo"}})
    assert r.status_code == 200 and not r.json()["ok"]
    assert r.json()["refused"] == {"session.permission_mode": "not one of: manual, plan"}, "the carabiner's words"

    b = c.get("/api/v1/carabiner/claude/belay").json()
    assert b["ok"] and b["checks"][0]["status"] == "held"


def test_only_a_carabiner_in_the_folder_runs(fake_home, monkeypatch, kbh_folder):
    c = _client(fake_home, monkeypatch, _on(kbh_folder))
    assert c.get("/api/v1/carabiner/hermes/settings").status_code == 404
    assert c.get("/api/v1/carabiner/..%2Fclaude/settings").status_code in (404, 405)


def test_the_yaml_block_is_read_like_ripple(tmp_path, monkeypatch):
    y = tmp_path / "obs.yaml"
    y.write_text("carabiners:\n  enabled: true\n  folder: /x/kbh\n  run_as: alice\n  nonsense: 1\n", encoding="utf-8")
    cfg = load_config(str(y))
    assert cfg.carabiners.enabled and cfg.carabiners.folder == "/x/kbh" and cfg.carabiners.run_as == "alice"
