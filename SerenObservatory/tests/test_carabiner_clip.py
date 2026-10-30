"""Clip and unclip through the Observatory (29 Oct 2026): a carabiner's
first file on this box arrives in the body like an upgrade and runs nothing;
an unclip keeps the file beside itself and the yaml where it is.
"""
from __future__ import annotations

import sys
import zipfile

from fastapi.testclient import TestClient

from seren_observatory.app import create_app
from seren_observatory.config import CarabinersConfig, ObservatoryConfig


def _client(monkeypatch, folder, token="tok"):
    monkeypatch.setattr("seren_observatory.auth.load_token", lambda *a, **k: token)
    monkeypatch.setattr("seren_observatory.app.load_token", lambda *a, **k: token)
    app = create_app(ObservatoryConfig(carabiners=CarabinersConfig(enabled=True, folder=str(folder),
                                                                   python=sys.executable)))
    return TestClient(app, headers={"Authorization": f"Bearer {token}"} if token else {})


def _kbh(path, version):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("__main__.py", "import sys; sys.exit(99)\n")
        zf.writestr("kbh/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr("kbh/upgrade.py", "")
    return path.read_bytes()


def test_a_clip_comes_in_the_body_is_checked_and_written_and_an_unclip_keeps_it(fake_home, monkeypatch, tmp_path):
    from seren_sinew.carabiners import UpgradeRequest
    folder = tmp_path / "zkbh"
    folder.mkdir()
    new = _kbh(tmp_path / "codex.kbh", "5.1.4")
    c = _client(monkeypatch, folder)
    assert c.get("/api/v1/carabiners").json()["carabiners"] == []

    req = UpgradeRequest.carrying(new, node="desktop", version="5.1.4", source="shelf")
    r = c.post("/api/v1/carabiner/codex/clip", json={**req.to_dict(), "sha256": "0" * 64})
    assert r.status_code == 200 and not r.json()["ok"] and "damaged" in r.json()["error"]
    r = c.post("/api/v1/carabiner/codex/clip", json=req.to_dict())
    assert r.status_code == 200 and r.json()["ok"] and r.json()["now"] == "5.1.4" and r.json()["changed"], r.json()
    assert (folder / "codex.kbh").read_bytes() == new
    listed = c.get("/api/v1/carabiners").json()["carabiners"]
    assert [e["carabiner"] for e in listed] == ["codex"] and not listed[0]["configured"]
    r = c.post("/api/v1/carabiner/codex/clip", json=req.to_dict())
    assert not r.json()["ok"] and "already clipped" in r.json()["error"]
    assert c.post("/api/v1/carabiner/Bad%20Name/clip", json=req.to_dict()).status_code == 404

    (folder / "codex.yaml").write_text("carabiner: codex\n", encoding="utf-8")
    r = c.post("/api/v1/carabiner/codex/unclip", json={"reason": "from the Rack"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["was"] == "5.1.4" and r.json()["yaml_kept"], r.json()
    assert r.json()["kept"].endswith("codex.kbh.unclipped-5.1.4") and "still registered" in r.json()["note"]
    assert not (folder / "codex.kbh").exists() and (folder / "codex.yaml").exists()
    assert c.post("/api/v1/carabiner/codex/unclip", json={}).status_code == 404, "gone is gone"
    assert _client(monkeypatch, folder, token=None).post("/api/v1/carabiner/codex/clip",
                                                        json=req.to_dict()).status_code in (403, 503)
