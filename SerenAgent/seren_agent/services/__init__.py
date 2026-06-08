"""
Per-service handler modules.

Each module under `agent.services.<name>` exposes a `register(router, name)`
function that maps service-specific endpoints onto the given FastAPI router.
The router is already prefixed with `/api/v1/service/{name}`, so handlers
just declare their sub-paths.

Example skeleton (see kokoro.py for a real one):

    def register(router):
        @router.get("/voices")
        async def list_voices(...): ...

The agent's main app calls these per-service register functions only for
services that have a manifest entry on this node - un-installed services
are silently absent from the API.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter

# Import each service's handler module here. They're optional - if a module
# is missing or fails to import, that service gets generic lifecycle
# endpoints only (start/stop/health/etc) without specifics.
from . import comfy, coral, kokoro, llama, whisper

# Map of service name → register(router) function
#
# NOTE: chroma was retired here. Memory is now its own standalone service
# (SerenMemory - github.com/ChadRoesler/SerenMemory) running off-cluster on
# a non-Jetson host, so the agent no longer manages a chroma service. See
# the topology docs. If a stale chroma manifest lingers on a node, the
# loader just gives it generic lifecycle endpoints (get_handler returns
# None) - harmless, but you should wipe it with seren-wipe.sh.
HANDLERS: dict[str, Callable[[APIRouter], None]] = {
    "comfy":   comfy.register,
    "coral":   coral.register,
    "kokoro":  kokoro.register,
    "llama":   llama.register,
    "whisper": whisper.register,
}


def get_handler(name: str) -> Callable[[APIRouter], None] | None:
    """Return the register function for the service, or None if no specific
    handlers exist for it (only generic lifecycle endpoints available)."""
    return HANDLERS.get(name)
