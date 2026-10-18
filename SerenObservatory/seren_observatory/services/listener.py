"""
Listener service-specific endpoints.

Mounted under /api/v1/service/listener.

The listener is the ear that understands (an audio-language model, Qwen3-Omni
thinker-only, behind seren-listener.py on the node): a clip and a question in,
words about the sound out - how a voice sounds, its tone and timbre, what a
song is doing. Whisper says what was said; this says how it sounded.

Endpoints:
    POST /listen       multipart `file` (a WAV, any rate), `prompt`?, `max_new_tokens`?
                       -> {ok, text, seconds, clip_seconds, cut} | {ok: false, error}
    POST /compare      multipart `a`, `b` (two WAVs), `prompt`?, `max_new_tokens`?
                       -> the same, about both

Both go to the listener on this box at the manifest's port, as the multipart
it takes. Through here and not straight to the port for the same reason as
whisper: the Observatory is the one door on a node, and Lodestar wakes the
listener through it (ensure) the way it wakes anything.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import manifests

LISTEN_DEFAULT_TIMEOUT = 180.0
LISTEN_MAX_TIMEOUT = 900.0
MAX_CLIP_BYTES = 64 * 1024 * 1024
# Tests put a MockTransport here; nothing else should.
TRANSPORT: httpx.AsyncBaseTransport | None = None


async def ask(port: int, path: str, files: dict[str, tuple[str, bytes, str]], fields: dict[str, str],
              timeout: float, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Post the clip(s) to the listener at 127.0.0.1:`port``path`; its answer,
    or why not. Never raises."""
    own = client is None
    client = client or httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=timeout, transport=TRANSPORT)
    t0 = time.monotonic()
    try:
        r = await client.post(path, files=files, data=fields)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
            detail = body.get("detail") or body.get("error") if isinstance(body, dict) else None
            return {"ok": False, "error": "the listener refused the clip" if r.status_code != 503 else "the listener is still loading",
                    "detail": detail or r.text[:2000]}
        return {**body, "seconds": body.get("seconds", round(time.monotonic() - t0, 2))}
    except httpx.HTTPError as ex:
        return {"ok": False, "error": "the listener did not answer", "detail": f"{type(ex).__name__}: {ex}"}
    finally:
        if own:
            await client.aclose()


def _timeout(value: float) -> float:
    return min(max(float(value or LISTEN_DEFAULT_TIMEOUT), 5.0), LISTEN_MAX_TIMEOUT)


async def _read(up: UploadFile, name: str) -> bytes:
    data = await up.read()
    if not data:
        raise HTTPException(400, f"send the clip as multipart `{name}`")
    if len(data) > MAX_CLIP_BYTES:
        raise HTTPException(413, f"the clip is bigger than {MAX_CLIP_BYTES // 1048576} MB")
    return data


def register(router: APIRouter) -> None:
    @router.post("/listen")
    async def listen_route(file: UploadFile = File(...), prompt: str = Form(""),
                           max_new_tokens: int = Form(400), timeout_seconds: float = Form(LISTEN_DEFAULT_TIMEOUT)):
        m = manifests.load_service("listener")
        if m is None:
            raise HTTPException(404, "listener not installed on this node")
        data = await _read(file, "file")
        out = await ask(int(m.get("port") or 8086), "/listen", {"file": (file.filename or "clip.wav", data, "audio/wav")},
                        {"prompt": prompt[:4000], "max_new_tokens": str(int(max_new_tokens))}, _timeout(timeout_seconds))
        if out.get("ok"):
            out["model"] = (m.get("serviceSpecific") or {}).get("model")
        return out

    @router.post("/compare")
    async def compare_route(a: UploadFile = File(...), b: UploadFile = File(...), prompt: str = Form(""),
                            max_new_tokens: int = Form(500), timeout_seconds: float = Form(LISTEN_DEFAULT_TIMEOUT)):
        m = manifests.load_service("listener")
        if m is None:
            raise HTTPException(404, "listener not installed on this node")
        files = {"a": (a.filename or "a.wav", await _read(a, "a"), "audio/wav"),
                 "b": (b.filename or "b.wav", await _read(b, "b"), "audio/wav")}
        out = await ask(int(m.get("port") or 8086), "/compare", files,
                        {"prompt": prompt[:4000], "max_new_tokens": str(int(max_new_tokens))}, _timeout(timeout_seconds))
        if out.get("ok"):
            out["model"] = (m.get("serviceSpecific") or {}).get("model")
        return out
