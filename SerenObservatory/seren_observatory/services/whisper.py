"""
Whisper service-specific endpoints.

Mounted under /api/v1/service/whisper.

Endpoints:
    GET  /models           - list installed model files (varies by impl)
    POST /transcribe       - words from a clip: multipart `file` (a WAV; the
                             server resamples), `language`?, `timeout_seconds`?
                             -> {ok, text, seconds, model} | {ok: false, error}

Note: implementation differs between Xavier (faster-whisper, HF cache) and
Nano (whisper.cpp, ggml-*.bin in source dir). The handler reads the manifest
to figure out which.

TRANSCRIBE goes to the whisper-server on this box at the manifest's port and
`endpoint` (prepare-node starts whisper.cpp with --inference-path
/v1/audio/transcriptions; the server's own default is /inference), as the
OpenAI-shaped multipart it takes, and asks for JSON. Why through here and not
straight to the port: the Observatory is the one door on a node, with the
token and the log, and Lodestar's `hear` route wakes whisper through it (ensure)
the way a drawing wakes ComfyUI. Callers used to go direct to 8081.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import manifests

DEFAULT_ENDPOINT = "/inference"
TRANSCRIBE_DEFAULT_TIMEOUT = 120.0
TRANSCRIBE_MAX_TIMEOUT = 900.0
# A clip longer than this is a file, not a thing said: refused before the
# server is asked (whisper.cpp holds the whole clip in memory).
MAX_CLIP_BYTES = 64 * 1024 * 1024
# What whisper.cpp writes for silence; nobody wants it in a composer.
BLANK = "[BLANK_AUDIO]"
# Tests put a MockTransport here; nothing else should.
TRANSPORT: httpx.AsyncBaseTransport | None = None


async def transcribe(port: int, endpoint: str, data: bytes, filename: str, language: str,
                     timeout: float, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Post the clip to the whisper-server at 127.0.0.1:`port` and return
    its words, or why not. Never raises."""
    own = client is None
    client = client or httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=timeout,
                                         transport=TRANSPORT)
    t0 = time.monotonic()
    fields: dict[str, Any] = {"response_format": "json", "temperature": "0"}
    if language:
        fields["language"] = language
    try:
        r = await client.post(endpoint, files={"file": (filename, data, "audio/wav")}, data=fields)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or not isinstance(body, dict) or "text" not in body:
            return {"ok": False, "error": "whisper refused the clip",
                    "detail": (body.get("error") if isinstance(body, dict) else None) or r.text[:2000]}
        text = " ".join(str(body.get("text") or "").replace(BLANK, " ").split())
        return {"ok": True, "text": text, "seconds": round(time.monotonic() - t0, 2)}
    except httpx.HTTPError as ex:
        return {"ok": False, "error": "whisper did not answer", "detail": f"{type(ex).__name__}: {ex}"}
    finally:
        if own:
            await client.aclose()


def register(router: APIRouter) -> None:
    @router.get("/models")
    async def list_models():
        manifest = manifests.load_service("whisper")
        if manifest is None:
            raise HTTPException(404, "whisper not installed on this node")

        impl = manifest.get("implementation", "")
        spec = manifest.get("serviceSpecific", {})

        # whisper.cpp: models live as ggml-*.bin beside the one the manifest
        # points at (prepare-node writes model_path, not source_dir), or in
        # <source_dir>/models for a manifest that still says source_dir.
        if "whisper.cpp" in impl:
            source_dir = spec.get("source_dir", "")
            model_path = spec.get("model_path", "")
            if model_path:
                models_dir = Path(model_path).parent
            elif source_dir:
                models_dir = Path(source_dir) / "models"
            else:
                models_dir = None
            if not models_dir or not models_dir.is_dir():
                return {"models": [], "implementation": impl,
                        "note": "models directory not found"}
            models = sorted(
                f.stem.removeprefix("ggml-")
                for f in models_dir.glob("ggml-*.bin")
            )
            return {
                "models": models,
                "current": spec.get("model"),
                "implementation": impl,
                "models_dir": str(models_dir),
            }

        # faster-whisper on Xavier: HuggingFace cache, harder to enumerate
        # without invoking the venv. Report just the configured one.
        if "faster-whisper" in impl:
            return {
                "models": [spec.get("model")] if spec.get("model") else [],
                "current": spec.get("model"),
                "implementation": impl,
                "note": "faster-whisper uses HF cache; full enumeration requires venv invocation",
            }

        return {"models": [], "implementation": impl,
                "note": "unknown whisper implementation"}

    @router.post("/transcribe")
    async def transcribe_route(file: UploadFile = File(...), language: str = Form(""),
                               timeout_seconds: float = Form(TRANSCRIBE_DEFAULT_TIMEOUT)):
        """multipart: file (WAV), language?, timeout_seconds? -> {ok, text,
        seconds, model} | {ok: false, error, detail}. The server must be up:
        ensure it first (Lodestar does, in its `hear` route)."""
        m = manifests.load_service("whisper")
        if m is None:
            raise HTTPException(404, "whisper not installed on this node")
        data = await file.read()
        if not data:
            raise HTTPException(400, "send the clip as multipart `file`")
        if len(data) > MAX_CLIP_BYTES:
            raise HTTPException(413, f"the clip is bigger than {MAX_CLIP_BYTES // 1048576} MB")
        timeout = min(max(float(timeout_seconds or TRANSCRIBE_DEFAULT_TIMEOUT), 5.0), TRANSCRIBE_MAX_TIMEOUT)
        out = await transcribe(int(m.get("port") or 8081), str(m.get("endpoint") or DEFAULT_ENDPOINT),
                               data, file.filename or "clip.wav", (language or "").strip()[:8], timeout)
        if out.get("ok"):
            out["model"] = (m.get("serviceSpecific") or {}).get("model")
        return out
