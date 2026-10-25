"""
Which carabiners are on the box (24 Oct 2026: "Observatory should be aware of
what carabiners are installed on the box"). The Rack's list is shut until
carabiners.enabled and reads only carabiners.folder, which nothing writes, so
a box with a clip on it read as having none.

- a clip the Starwright card put anywhere is found through the install ledger
- ~/seren-kbh, the card's default, is found with no ledger and no config
- reading one runs nothing: the version comes out of the zip
- one entry per .kbh, however many places found it; rack says the Rack can reach it
- a ledger record whose .kbh is gone is listed, present false
- /system/node carries them, so Lodestar's poll does; the bearer still guards both
"""
from __future__ import annotations

import json
import zipfile

from fastapi.testclient import TestClient

from seren_observatory.app import create_app
from seren_observatory.config import CarabinersConfig, ObservatoryConfig

# Would leave a mark if anything ran it: the inventory must only read it.
RUNS = 'import os, sys\nopen(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "RAN"), "w").write("x")\n'


def _kbh(path, version="5.1.3", build="1f116ed"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("__main__.py", RUNS)
        zf.writestr("kbh/__init__.py", f'__version__ = "{version}"\n')
        if build:
            zf.writestr("kbh/_build.py", f'BUILD = "{build}"\n')
    return path


def _ledger(home, name, **rec):
    d = home / ".seren" / "installed"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({"schema_version": 1, "service": "seren-carabiner", **rec}),
                                    encoding="utf-8")


def _client(monkeypatch, carabiners: CarabinersConfig | None = None, token=None):
    monkeypatch.setattr("seren_observatory.auth.load_token", lambda *a, **k: token)
    monkeypatch.setattr("seren_observatory.app.load_token", lambda *a, **k: token)
    app = create_app(ObservatoryConfig(carabiners=carabiners or CarabinersConfig()))
    return TestClient(app, headers={"Authorization": f"Bearer {token}"} if token else {})


def test_a_clip_the_card_put_anywhere_is_found_through_the_ledger(fake_home, monkeypatch, node_manifest, tmp_path):
    clip = tmp_path / "seren" / "wren" / "kbh"
    _kbh(clip / "claude.kbh")
    (clip / "claude.yaml").write_text("carabiner: claude\n", encoding="utf-8")
    _ledger(fake_home, "seren-carabiner@wren", instance="wren", config=str(clip / "claude.yaml"), app_dir=str(clip),
            source="release", source_ref="v5.1.3", installed_at="2026-10-23T18:00:00Z",
            options={"project": "/home/alice/work", "instance": "wren"})
    c = _client(monkeypatch)                       # carabiners off, no folder: the Rack sees nothing
    assert c.get("/api/v1/carabiners").status_code in (403, 409)

    r = c.get("/api/v1/system/carabiners").json()
    assert r["ok"] and r["count"] == 1, r
    e = r["carabiners"][0]
    assert e["carabiner"] == "claude" and e["node"] == "test-jetson" and e["version"] == "5.1.3+1f116ed"
    assert len(e["sha256"]) == 64 and e["configured"] and e["present"] and not e["rack"]
    assert e["kbh"].endswith("claude.kbh") and e["found_by"] == ["ledger"]
    assert e["install"]["instance"] == "wren" and e["install"]["source_ref"] == "v5.1.3"
    assert e["install"]["project"] == "/home/alice/work" and e["install"]["installed_at"].startswith("2026-10-23")
    assert not (clip / "RAN").exists(), "listing a carabiner must not run it"


def test_the_cards_default_folder_is_found_with_no_ledger(fake_home, monkeypatch):
    _kbh(fake_home / "seren-kbh" / "claude.kbh", build="")
    rows = _client(monkeypatch).get("/api/v1/system/carabiners").json()["carabiners"]
    assert [(e["carabiner"], e["version"], e["configured"], e["found_by"], e["install"]) for e in rows] \
        == [("claude", "5.1.3", False, ["default"], None)]


def test_one_entry_per_kbh_and_rack_says_the_rack_reaches_it(fake_home, monkeypatch, tmp_path):
    folder = tmp_path / "kbh"
    _kbh(folder / "claude.kbh")
    _kbh(folder / "hermes.kbh", version="0.2.0")
    _ledger(fake_home, "seren-carabiner", config=str(folder / "claude.yaml"), app_dir=str(folder),
            installed_at="2026-10-22T00:00:00Z")
    rows = _client(monkeypatch, CarabinersConfig(enabled=True, folder=str(folder))).get(
        "/api/v1/system/carabiners").json()["carabiners"]
    assert [e["carabiner"] for e in rows] == ["claude", "hermes"]
    claude, hermes = rows
    assert claude["rack"] and hermes["rack"]
    assert claude["found_by"] == ["folder", "ledger"] and claude["install"] is not None
    assert hermes["install"] is None, "the record names claude, not every clip in its folder"

    off = _client(monkeypatch, CarabinersConfig(enabled=False, folder=str(folder))).get(
        "/api/v1/system/carabiners").json()["carabiners"]
    assert len(off) == 2 and not any(e["rack"] for e in off), "listed, but the Rack is off"


def test_a_ledger_record_whose_kbh_is_gone_is_listed_missing(fake_home, monkeypatch, tmp_path):
    gone = tmp_path / "gone"
    _ledger(fake_home, "seren-carabiner@old", instance="old", config=str(gone / "claude.yaml"), app_dir=str(gone))
    rows = _client(monkeypatch).get("/api/v1/system/carabiners").json()["carabiners"]
    assert len(rows) == 1 and rows[0]["carabiner"] == "claude" and not rows[0]["present"]
    assert rows[0]["version"] == "" and rows[0]["install"]["instance"] == "old"


def test_other_records_and_broken_ones_are_passed_over(fake_home, monkeypatch, tmp_path):
    d = fake_home / ".seren" / "installed"
    d.mkdir(parents=True)
    (d / "seren-carabiner@bad.json").write_text("{not json", encoding="utf-8")
    (d / "seren-memory.json").write_text(json.dumps({"service": "seren-memory", "app_dir": str(tmp_path)}),
                                         encoding="utf-8")
    _ledger(fake_home, "seren-carabiner@noname", app_dir=str(tmp_path))      # says no carabiner
    r = _client(monkeypatch).get("/api/v1/system/carabiners").json()
    assert r["ok"] and r["carabiners"] == []
    assert any(p.endswith("seren-kbh") for p in r["looked_in"])


def test_the_ledger_moves_with_seren_installed_dir(fake_home, monkeypatch, tmp_path):
    clip = tmp_path / "clip"
    _kbh(clip / "claude.kbh")
    elsewhere = tmp_path / "ledger"
    elsewhere.mkdir()
    (elsewhere / "seren-carabiner.json").write_text(json.dumps(
        {"service": "seren-carabiner", "config": str(clip / "claude.yaml")}), encoding="utf-8")
    monkeypatch.setenv("SEREN_INSTALLED_DIR", str(elsewhere))
    rows = _client(monkeypatch).get("/api/v1/system/carabiners").json()["carabiners"]
    assert [e["carabiner"] for e in rows] == ["claude"] and rows[0]["present"]


def test_system_node_carries_them_for_lodestars_poll(fake_home, monkeypatch, node_manifest):
    c = _client(monkeypatch)
    assert c.get("/api/v1/system/node").json()["carabiners"] == []
    _kbh(fake_home / "seren-kbh" / "claude.kbh")
    n = c.get("/api/v1/system/node").json()
    assert n["manifest"]["hostname"] == "test-jetson"
    assert [(e["carabiner"], e["version"]) for e in n["carabiners"]] == [("claude", "5.1.3+1f116ed")]


def test_a_node_with_a_token_wants_it_here_too(fake_home, monkeypatch):
    c = _client(monkeypatch, token="tok")
    assert c.get("/api/v1/system/carabiners").status_code == 200
    assert TestClient(c.app).get("/api/v1/system/carabiners").status_code == 401


def test_an_inventory_that_fails_never_costs_the_node(fake_home, monkeypatch, node_manifest):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr("seren_observatory.carabiner_inventory.installed", boom)
    c = _client(monkeypatch)
    n = c.get("/api/v1/system/node")
    assert n.status_code == 200 and n.json()["carabiners"] == [] and n.json()["manifest"]["hostname"] == "test-jetson"
    r = c.get("/api/v1/system/carabiners").json()
    assert not r["ok"] and "disk on fire" in r["error"]
