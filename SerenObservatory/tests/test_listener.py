"""
The listener on a node: a clip and a question to the audio-language model
behind seren-listener.py, through the Observatory's door.
"""
from __future__ import annotations

import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory.app import create_app
from seren_observatory.services import listener


def _manifest(fake_home) -> None:
    (fake_home / ".seren" / "services" / "listener.json").write_text(json.dumps({
        "schema_version": 1, "service": "listener", "service_type": "pid_file", "port": 8086,
        "endpoint": "/listen", "implementation": "qwen3-omni",
        "serviceSpecific": {"model": "Qwen/Qwen3-Omni-30B-A3B-Instruct"}}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "tok"}))


def _server(seen: dict, text="A warm, low alto; unhurried; a little breath on the vowels.", status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), body=request.read())
        if status == 503:
            return httpx.Response(503, json={"detail": "still loading"})
        if status != 200:
            return httpx.Response(status, json={"detail": "not a clip I can read"})
        return httpx.Response(200, json={"ok": True, "text": text, "seconds": 3.2, "clip_seconds": 4.0, "cut": False})
    return httpx.MockTransport(handle)


async def _post(fake_home, path, files, **form):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test",
                           headers={"Authorization": "Bearer tok"}) as c:
        return await c.post(f"/api/v1/service/listener/{path}", files=files, data=form)


async def test_a_clip_and_a_question_reach_the_listener_and_its_words_come_back(fake_home, monkeypatch):
    _manifest(fake_home)
    seen: dict = {}
    monkeypatch.setattr(listener, "TRANSPORT", _server(seen))
    r = await _post(fake_home, "listen", {"file": ("me.wav", b"RIFF-wav-bytes", "audio/wav")},
                    prompt="how does this voice sound?")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["text"].startswith("A warm, low alto") and body["model"] == "Qwen/Qwen3-Omni-30B-A3B-Instruct"
    assert body["clip_seconds"] == 4.0 and body["seconds"] == 3.2
    assert seen["url"] == "http://127.0.0.1:8086/listen"
    for part in (b'name="file"; filename="me.wav"', b"RIFF-wav-bytes", b'name="prompt"\r\n\r\nhow does this voice sound?',
                 b'name="max_new_tokens"\r\n\r\n400'):
        assert part in seen["body"], part


async def test_two_clips_are_compared(fake_home, monkeypatch):
    _manifest(fake_home)
    seen: dict = {}
    monkeypatch.setattr(listener, "TRANSPORT", _server(seen, text="B is brighter and a touch faster."))
    r = await _post(fake_home, "compare", {"a": ("a.wav", b"AAAA", "audio/wav"), "b": ("b.wav", b"BBBB", "audio/wav")})
    assert r.status_code == 200 and r.json()["text"] == "B is brighter and a touch faster."
    assert seen["url"] == "http://127.0.0.1:8086/compare"
    assert b'name="a"; filename="a.wav"' in seen["body"] and b'name="b"; filename="b.wav"' in seen["body"]


async def test_a_listener_still_loading_or_refusing_or_down_is_an_answer(fake_home, monkeypatch):
    _manifest(fake_home)
    monkeypatch.setattr(listener, "TRANSPORT", _server({}, status=503))
    body = (await _post(fake_home, "listen", {"file": ("x.wav", b"x", "audio/wav")})).json()
    assert body["ok"] is False and body["error"] == "the listener is still loading"
    monkeypatch.setattr(listener, "TRANSPORT", _server({}, status=400))
    body = (await _post(fake_home, "listen", {"file": ("x.wav", b"x", "audio/wav")})).json()
    assert body["ok"] is False and body["error"] == "the listener refused the clip" and "not a clip" in body["detail"]

    def down(request):
        raise httpx.ConnectError("refused", request=request)
    monkeypatch.setattr(listener, "TRANSPORT", httpx.MockTransport(down))
    body = (await _post(fake_home, "listen", {"file": ("x.wav", b"x", "audio/wav")})).json()
    assert body["ok"] is False and body["error"] == "the listener did not answer"


async def test_nothing_is_sent_without_a_manifest_or_a_clip(fake_home, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(listener, "TRANSPORT", _server(seen))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "tok"}))
    assert (await _post(fake_home, "listen", {"file": ("x.wav", b"x", "audio/wav")})).status_code == 404
    _manifest(fake_home)
    assert (await _post(fake_home, "listen", {"file": ("x.wav", b"", "audio/wav")})).status_code == 400
    assert (await _post(fake_home, "compare", {"a": ("a.wav", b"A", "audio/wav"), "b": ("b.wav", b"", "audio/wav")})).status_code == 400
    assert not seen
