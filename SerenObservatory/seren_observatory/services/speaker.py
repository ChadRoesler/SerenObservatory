"""
Speaker service-specific endpoints.

Mounted under /api/v1/service/speaker.

The speaker is a voice (seren-speaker.py on the node: a reference clip and
its transcript, cloned by a text-to-speech backend for whatever it is asked
to say). Words in, a WAV out. Kokoro is a voice too, but a borrowed one
with a name from a list; the speaker says it in the voice that was chosen.

Endpoints:
    POST /speak        JSON {text, voice?, timeout_seconds?}
                       -> audio/wav, with X-Seren-Voice, X-Seren-Backend,
                          X-Seren-Seconds (to make it), X-Seren-Clip-Seconds
                       | JSON {ok: false, error, detail} with 503 (loading,
                          down) or 502 (refused)
    GET  /voices       -> {voices: [{name, backend, language, ref_seconds}]}

Through here and not straight to the port for the same reason as the
listener: the Observatory is the one door on a node, and Lodestar wakes the
speaker through it (ensure) the way it wakes anything.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .. import manifests

SPEAK_DEFAULT_TIMEOUT = 120.0
SPEAK_MAX_TIMEOUT = 600.0
MAX_TEXT_CHARS = 4000
PASS_HEADERS = ("x-seren-voice", "x-seren-backend", "x-seren-seconds", "x-seren-clip-seconds", "x-seren-chunks")
# Tests put a MockTransport here; nothing else should.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class SpeakBody(BaseModel):
    text: str
    voice: str = "wren"
    timeout_seconds: float = SPEAK_DEFAULT_TIMEOUT


def _timeout(value: float) -> float:
    return min(max(float(value or SPEAK_DEFAULT_TIMEOUT), 5.0), SPEAK_MAX_TIMEOUT)


async def say(port: int, text: str, voice: str, timeout: float,
              client: httpx.AsyncClient | None = None) -> tuple[int, bytes | dict[str, Any], dict[str, str]]:
    """Ask the speaker at 127.0.0.1:`port` to say `text` in `voice`:
    (200, wav bytes, headers) or (503|502, {ok: false, error, detail}, {}).
    Never raises."""
    own = client is None
    client = client or httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=timeout, transport=TRANSPORT)
    t0 = time.monotonic()
    try:
        r = await client.post("/speak", json={"text": text, "voice": voice})
        if r.status_code == 200 and r.content.startswith(b"RIFF"):
            # httpx lowercases header names; keep them lowercase here too, or
            # the default below sits beside the speaker's own and the caller
            # sees "10.46, 10.47" (17 Oct 2026).
            headers = {k.lower(): v for k, v in r.headers.items() if k.lower() in PASS_HEADERS}
            headers.setdefault("x-seren-seconds", str(round(time.monotonic() - t0, 2)))
            return 200, r.content, headers
        try:
            body = r.json()
        except ValueError:
            body = {}
        detail = (body.get("detail") or body.get("error")) if isinstance(body, dict) else None
        if r.status_code == 503:
            return 503, {"ok": False, "error": "the speaker is still loading", "detail": detail or r.text[:500]}, {}
        return 502, {"ok": False, "error": "the speaker refused the text", "detail": detail or r.text[:2000]}, {}
    except httpx.HTTPError as ex:
        return 503, {"ok": False, "error": "the speaker did not answer", "detail": f"{type(ex).__name__}: {ex}"}, {}
    finally:
        if own:
            await client.aclose()


async def voices(port: int, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    own = client is None
    client = client or httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10.0, transport=TRANSPORT)
    try:
        r = await client.get("/voices")
        body = r.json() if r.status_code == 200 else {}
        if not isinstance(body, dict) or "voices" not in body:
            return {"ok": False, "error": "the speaker did not list its voices", "detail": r.text[:500]}
        return {"ok": True, "voices": body["voices"]}
    except (httpx.HTTPError, ValueError) as ex:
        return {"ok": False, "error": "the speaker did not answer", "detail": f"{type(ex).__name__}: {ex}"}
    finally:
        if own:
            await client.aclose()


def register(router: APIRouter) -> None:
    @router.post("/speak")
    async def speak_route(body: SpeakBody):
        m = manifests.load_service("speaker")
        if m is None:
            raise HTTPException(404, "speaker not installed on this node")
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(400, "send `text` to say")
        if len(text) > MAX_TEXT_CHARS:
            raise HTTPException(413, f"that is {len(text)} characters; at most {MAX_TEXT_CHARS} in one go")
        status, out, headers = await say(int(m.get("port") or 8087), text, (body.voice or "wren").strip()[:64],
                                         _timeout(body.timeout_seconds))
        if status != 200:
            return JSONResponse(out, status_code=status)
        headers["x-seren-model"] = str((m.get("serviceSpecific") or {}).get("model") or "")
        return Response(content=out, media_type="audio/wav", headers=headers)

    @router.get("/voices")
    async def voices_route():
        m = manifests.load_service("speaker")
        if m is None:
            raise HTTPException(404, "speaker not installed on this node")
        out = await voices(int(m.get("port") or 8087))
        if out.get("ok"):
            out["model"] = (m.get("serviceSpecific") or {}).get("model")
        return out
