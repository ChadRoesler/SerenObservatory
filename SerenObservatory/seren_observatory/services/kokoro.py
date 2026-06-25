"""
Kokoro service-specific endpoints.

Mounted under /api/v1/service/kokoro.

Endpoints:
    GET    /voices                - list installed voice files
    DELETE /voices/{name}         - remove an installed voice
    POST   /synthesize            - proxy to kokoro-fastapi (TODO)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import manifests


def register(router: APIRouter) -> None:
    @router.get("/voices")
    async def list_voices():
        manifest = manifests.load_service("kokoro")
        if manifest is None:
            raise HTTPException(404, "kokoro not installed on this node")

        voices_path = manifest.get("serviceSpecific", {}).get("voices_path")
        if not voices_path:
            raise HTTPException(500, "kokoro manifest missing serviceSpecific.voices_path")

        voices_dir = Path(voices_path)
        if not voices_dir.is_dir():
            return {"voices": [], "note": f"{voices_path} does not exist yet"}

        # Kokoro voice files are .pt (PyTorch state dicts)
        voices = sorted(p.stem for p in voices_dir.glob("*.pt"))
        return {"voices": voices, "voices_path": str(voices_dir)}

    @router.delete("/voices/{name}")
    async def delete_voice(name: str):
        manifest = manifests.load_service("kokoro")
        if manifest is None:
            raise HTTPException(404, "kokoro not installed on this node")

        # Defense against path traversal - voice names must be plain identifiers
        if "/" in name or ".." in name or name.startswith("."):
            raise HTTPException(400, "invalid voice name")

        voices_path = manifest.get("serviceSpecific", {}).get("voices_path", "")
        target = Path(voices_path) / f"{name}.pt"
        if not target.is_file():
            raise HTTPException(404, f"voice {name} not found")

        target.unlink()
        return {"ok": True, "removed": name}

    # TODO Tier 2: POST /synthesize that proxies to kokoro-fastapi server.
    # Currently the chat app talks to kokoro directly on its port. When we
    # add observatory-side auth/logging/rate-limiting, this becomes the single
    # entry point.
