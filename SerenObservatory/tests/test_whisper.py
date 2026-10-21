"""
whisper on a node: which models are actually there.

FOUND 16 Oct 2026: prepare-node writes `model_path` into the whisper
manifest and the models route looked only for `source_dir`, so a node with
ggml-small.en.bin sitting right there answered "models directory not found"
and `list_models("whisper")` through the Workbench said the same.
"""
from __future__ import annotations

import json

import httpx
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


# ── transcribe: the clip goes to the whisper-server on this box ──────────────

def _whisper_server(seen: dict, text: str = " Hello there. [BLANK_AUDIO]", status: int = 200):
    """A scripted whisper-server: records what it was sent, answers `text`."""
    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), method=request.method, content_type=request.headers.get("content-type", ""))
        seen["body"] = request.read()
        return httpx.Response(status, json={"text": text} if status == 200 else {"error": "no model loaded"})
    return httpx.MockTransport(handle)


async def _post_clip(fake_home, data=b"RIFF....WAVEfmt fake", **form):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test",
                           headers={"Authorization": "Bearer tok"}) as c:
        return await c.post("/api/v1/service/whisper/transcribe", files={"file": ("clip.wav", data, "audio/wav")},
                            data=form)


async def test_a_clip_is_forwarded_to_the_servers_endpoint_and_the_words_come_back(fake_home, monkeypatch):
    from seren_observatory.services import whisper
    _manifest(fake_home, {"model": "small.en"})
    m = json.loads((fake_home / ".seren" / "services" / "whisper.json").read_text())
    m["endpoint"] = "/v1/audio/transcriptions"            # what prepare-node starts the server with
    (fake_home / ".seren" / "services" / "whisper.json").write_text(json.dumps(m))
    seen: dict = {}
    monkeypatch.setattr(whisper, "TRANSPORT", _whisper_server(seen))
    r = await _post_clip(fake_home, language="en")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["text"] == "Hello there." and body["model"] == "small.en", "the silence marker is dropped"
    assert seen["url"] == "http://127.0.0.1:8081/v1/audio/transcriptions"
    assert seen["content_type"].startswith("multipart/form-data")
    for part in (b'name="file"; filename="clip.wav"', b"RIFF....WAVEfmt fake",
                 b'name="response_format"\r\n\r\njson', b'name="language"\r\n\r\nen'):
        assert part in seen["body"], part


async def test_a_manifest_without_an_endpoint_uses_the_servers_default(fake_home, monkeypatch):
    from seren_observatory.services import whisper
    _manifest(fake_home, {"model": "small.en"})
    seen: dict = {}
    monkeypatch.setattr(whisper, "TRANSPORT", _whisper_server(seen))
    assert (await _post_clip(fake_home)).json()["ok"]
    assert seen["url"] == "http://127.0.0.1:8081/inference"
    assert b'name="language"' not in seen["body"], "no language asked, none sent"


async def test_a_server_that_refuses_or_is_down_is_an_answer_not_a_crash(fake_home, monkeypatch):
    from seren_observatory.services import whisper
    _manifest(fake_home, {"model": "small.en"})
    monkeypatch.setattr(whisper, "TRANSPORT", _whisper_server({}, status=500))
    body = (await _post_clip(fake_home)).json()
    assert body["ok"] is False and body["error"] == "whisper refused the clip" and "no model loaded" in body["detail"]

    def down(request):
        raise httpx.ConnectError("refused", request=request)
    monkeypatch.setattr(whisper, "TRANSPORT", httpx.MockTransport(down))
    body = (await _post_clip(fake_home)).json()
    assert body["ok"] is False and body["error"] == "whisper did not answer"


async def test_an_empty_clip_and_a_missing_whisper_are_refused_before_anything_is_sent(fake_home, monkeypatch):
    from seren_observatory.services import whisper
    asked = []
    monkeypatch.setattr(whisper, "TRANSPORT", _whisper_server(asked))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "tok"}))
    assert (await _post_clip(fake_home)).status_code == 404, "no manifest: whisper is not on this node"
    _manifest(fake_home, {"model": "small.en"})
    assert (await _post_clip(fake_home, data=b"")).status_code == 400
    assert not asked
