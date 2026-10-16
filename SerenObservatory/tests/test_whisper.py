"""
whisper on a node: which models are actually there.

FOUND 16 Oct 2026: prepare-node writes `model_path` into the whisper
manifest and the models route looked only for `source_dir`, so a node with
ggml-small.en.bin sitting right there answered "models directory not found"
and `list_models("whisper")` through the Workbench said the same.
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory.app import create_app


def _manifest(fake_home, spec: dict) -> None:
    (fake_home / ".seren" / "services" / "whisper.json").write_text(json.dumps({
        "schema_version": 1, "service": "whisper", "service_type": "pid_file", "port": 8081,
        "implementation": "whisper.cpp", "serviceSpecific": spec}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "tok"}))


@pytest.fixture()
def models(tmp_path):
    d = tmp_path / "nvme" / "models" / "whisper"
    d.mkdir(parents=True)
    (d / "ggml-small.en.bin").write_bytes(b"\0" * 16)
    (d / "ggml-medium.en.bin").write_bytes(b"\0" * 16)
    (d / "notes.txt").write_text("not a model")
    return d


async def _get(fake_home):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test",
                           headers={"Authorization": "Bearer tok"}) as c:
        return await c.get("/api/v1/service/whisper/models")


async def test_models_are_found_beside_the_manifests_model_path(fake_home, models):
    """The shape prepare-node actually writes: model_path, no source_dir."""
    _manifest(fake_home, {"model": "small.en", "model_path": str(models / "ggml-small.en.bin"), "device": "cuda"})
    r = await _get(fake_home)
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == ["medium.en", "small.en"]
    assert body["current"] == "small.en"
    assert "note" not in body


async def test_a_manifest_that_still_says_source_dir_keeps_working(fake_home, models):
    src = models.parent.parent / "whisper.cpp"
    (src / "models").mkdir(parents=True)
    (src / "models" / "ggml-base.bin").write_bytes(b"\0")
    _manifest(fake_home, {"model": "base", "source_dir": str(src)})
    r = await _get(fake_home)
    assert r.json()["models"] == ["base"]


async def test_no_path_at_all_is_reported_not_raised(fake_home):
    _manifest(fake_home, {"model": "small.en"})
    r = await _get(fake_home)
    assert r.status_code == 200 and r.json()["note"] == "models directory not found"
