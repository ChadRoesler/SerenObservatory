"""
Whisper service-specific endpoints.

Mounted under /api/v1/service/whisper.

Endpoints:
    GET  /models           - list installed model files (varies by impl)
    POST /transcribe       - proxy multipart upload to whisper-server (TODO)

Note: implementation differs between Xavier (faster-whisper, HF cache) and
Nano (whisper.cpp, ggml-*.bin in source dir). The handler reads the manifest
to figure out which.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import manifests


def register(router: APIRouter) -> None:
    @router.get("/models")
    async def list_models():
        manifest = manifests.load_service("whisper")
        if manifest is None:
            raise HTTPException(404, "whisper not installed on this node")

        impl = manifest.get("implementation", "")
        spec = manifest.get("serviceSpecific", {})

        # whisper.cpp on Nano: models live as ggml-*.bin in <source_dir>/models
        if "whisper.cpp" in impl:
            source_dir = spec.get("source_dir", "")
            models_dir = Path(source_dir) / "models" if source_dir else None
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

    # TODO Tier 2: POST /transcribe - proxy multipart audio upload to the
    # whisper-server's /v1/audio/transcriptions. Right now callers go direct
    # to port 8081. Agent-side proxy gives us auth + logging.
