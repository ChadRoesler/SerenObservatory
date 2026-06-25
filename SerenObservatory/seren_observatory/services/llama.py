"""
llama.cpp service-specific endpoints.

Mounted under /api/v1/service/llama.

Endpoints:
    GET  /models           - list .gguf files in the configured models dir
    POST /completion       - proxy to llama-server /completion (TODO)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import manifests


def register(router: APIRouter) -> None:
    @router.get("/models")
    async def list_models():
        manifest = manifests.load_service("llama")
        if manifest is None:
            raise HTTPException(404, "llama not installed on this node")

        models_dir = manifest.get("serviceSpecific", {}).get("models_dir", "")
        if not models_dir:
            raise HTTPException(500, "llama manifest missing serviceSpecific.models_dir")

        p = Path(models_dir)
        if not p.is_dir():
            return {
                "models": [],
                "models_dir": str(p),
                "note": "models directory does not exist yet",
            }

        models = sorted(f.name for f in p.glob("*.gguf"))
        # File sizes are useful for the dashboard ("how much disk is each eating")
        with_sizes = []
        for name in models:
            f = p / name
            try:
                size_mb = f.stat().st_size // (1024 * 1024)
            except OSError:
                size_mb = None
            with_sizes.append({"name": name, "size_mb": size_mb})

        return {"models": with_sizes, "models_dir": str(p)}

    # TODO Tier 2: POST /completion - proxy to llama-server's /completion
    # endpoint with token streaming. The chat app currently goes direct.
