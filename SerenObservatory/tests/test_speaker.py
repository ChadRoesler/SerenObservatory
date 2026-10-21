"""
The speaker on a node: words to the voice behind seren-speaker.py, a WAV
back, through the Observatory's door.
"""
from __future__ import annotations

import json

import httpx
from httpx import ASGITransport, AsyncClient

from seren_observatory.app import create_app
from seren_observatory.services import speaker

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 20 + b"data\0\0\0\0"


def _manifest(fake_home) -> None:
    (fake_home / ".seren" / "services" / "speaker.json").write_text(json.dumps({
        "schema_version": 1, "service": "speaker", "service_type": "pid_file", "port": 8087,
        "endpoint": "/speak", "implementation": "qwen3-tts",
        "serviceSpecific": {"model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base"}}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "tok"}))


def _server(seen: dict, status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), body=request.read())
        if request.url.path == "/voices":
            return httpx.Response(200, json={"voices": [{"name": "wren", "backend": "qwen3tts", "language": "English",
                                                         "ref_seconds": 19.7}]})
        if status == 503:
            return httpx.Response(503, json={"detail": "still loading"})
        if status != 200:
            return httpx.Response(status, json={"detail": "no voice 'bob'; I have ['wren']"})
        return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav", "X-Seren-Voice": "wren",
                                                         "X-Seren-Backend": "qwen3tts", "X-Seren-Seconds": "4.1",
                                                         "X-Seren-Clip-Seconds": "4.9", "X-Seren-Chunks": "1"})
    return httpx.MockTransport(handle)


async def _client(fake_home):
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test",
                       headers={"Authorization": "Bearer tok"})


async def test_words_reach_the_speaker_and_its_voice_comes_back_as_a_wav(fake_home, monkeypatch):
    _manifest(fake_home)
    seen: dict = {}
    monkeypatch.setattr(speaker, "TRANSPORT", _server(seen))
    async with await _client(fake_home) as c:
        r = await c.post("/api/v1/service/speaker/speak", json={"text": "Hi, it's me."})
    assert r.status_code == 200, r.text
    assert r.content == WAV and r.headers["content-type"].startswith("audio/wav")
    assert r.headers["x-seren-voice"] == "wren" and r.headers["x-seren-backend"] == "qwen3tts"
    assert r.headers["x-seren-clip-seconds"] == "4.9" and r.headers["x-seren-model"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    assert r.headers.get_list("x-seren-seconds") == ["4.1"], "the speaker's own timing, once - not beside a default"
    assert seen["url"] == "http://127.0.0.1:8087/speak"
    assert json.loads(seen["body"]) == {"text": "Hi, it's me.", "voice": "wren"}


async def test_the_voices_are_listed(fake_home, monkeypatch):
    _manifest(fake_home)
    monkeypatch.setattr(speaker, "TRANSPORT", _server({}))
    async with await _client(fake_home) as c:
        r = await c.get("/api/v1/service/speaker/voices")
    assert r.status_code == 200 and r.json()["voices"][0]["name"] == "wren"
    assert r.json()["model"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"


async def test_a_speaker_still_loading_or_refusing_or_down_is_an_answer_with_a_status(fake_home, monkeypatch):
    _manifest(fake_home)
    monkeypatch.setattr(speaker, "TRANSPORT", _server({}, status=503))
    async with await _client(fake_home) as c:
        r = await c.post("/api/v1/service/speaker/speak", json={"text": "hi"})
    assert r.status_code == 503 and r.json()["error"] == "the speaker is still loading"
    monkeypatch.setattr(speaker, "TRANSPORT", _server({}, status=404))
    async with await _client(fake_home) as c:
        r = await c.post("/api/v1/service/speaker/speak", json={"text": "hi", "voice": "bob"})
    assert r.status_code == 502 and r.json()["error"] == "the speaker refused the text" and "no voice 'bob'" in r.json()["detail"]

    def down(request):
        raise httpx.ConnectError("refused", request=request)
    monkeypatch.setattr(speaker, "TRANSPORT", httpx.MockTransport(down))
    async with await _client(fake_home) as c:
        r = await c.post("/api/v1/service/speaker/speak", json={"text": "hi"})
    assert r.status_code == 503 and r.json()["error"] == "the speaker did not answer"


async def test_nothing_is_sent_without_a_manifest_or_text(fake_home, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(speaker, "TRANSPORT", _server(seen))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "tok"}))
    async with await _client(fake_home) as c:
        assert (await c.post("/api/v1/service/speaker/speak", json={"text": "hi"})).status_code == 404
        _manifest(fake_home)
        assert (await c.post("/api/v1/service/speaker/speak", json={"text": "   "})).status_code == 400
        assert (await c.post("/api/v1/service/speaker/speak", json={"text": "x" * 4001})).status_code == 413
    assert not seen
