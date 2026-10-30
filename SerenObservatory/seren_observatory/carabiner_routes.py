"""
The carabiners clipped on this box, for Lodestar's Rack
(seren_sinew.carabiners has the chain and the messages).

    GET  /api/v1/carabiners                       every <name>.kbh in carabiners.folder
    GET  /api/v1/carabiner/{name}/settings        its settings, as the carabiner says them
    POST /api/v1/carabiner/{name}/settings        SettingsRequest -> SettingsResult
    GET  /api/v1/carabiner/{name}/belay           does it still hold
    POST /api/v1/carabiner/{name}/upgrade         UpgradeRequest -> UpgradeResult: a new .kbh, sent
                                                  by Lodestar, checked and swapped in by kbh itself
    POST /api/v1/carabiner/{name}/clip            UpgradeRequest -> UpgradeResult: a carabiner's FIRST
                                                  file here, checked and written; nothing of it runs
    POST /api/v1/carabiner/{name}/unclip          {"reason"} -> UnclipResult: the .kbh kept beside
                                                  itself, the yaml left, nothing unregistered

The Observatory carries; the carabiner decides. Each call runs the .kbh AS
the person whose yaml it is and passes on what it answered: which settings
exist, which values are refused and why. Off until carabiners.enabled, and
closed even to a GET on an Observatory with no token, because every call
here starts a program in someone's account.
"""
from __future__ import annotations

import asyncio
import socket
import sys
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from seren_sinew.carabiners import CarabinerEntry, KbhRunner, SettingsRequest, UpgradeRequest

from . import manifests

router = APIRouter(prefix="/api/v1", tags=["carabiners"])


def make_runner(cfg) -> Optional[KbhRunner]:
    """The box's KbhRunner, or None while carabiners are off."""
    c = cfg.carabiners
    if not c.enabled:
        return None
    node = (manifests.load_node() or {}).get("hostname") or socket.gethostname()
    return KbhRunner(folder=c.folder, python=c.python or sys.executable, run_as=c.run_as or cfg.ripple.run_as,
                     timeout_seconds=c.timeout_seconds, upgrade_timeout_seconds=c.upgrade_timeout_seconds,
                     node=str(node))


def _gate(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.app.state, "has_token", False):
        return JSONResponse({"ok": False, "error": "carabiners need this Observatory's token set: every call here "
                                                   "runs a program in someone's account"}, status_code=403)
    if getattr(request.app.state, "kbh", None) is None:
        return JSONResponse({"ok": False, "error": "carabiners are not set up on this node "
                                                   "(carabiners.enabled is false)"}, status_code=409)
    return None


def _answer(result: Any) -> JSONResponse:
    d = result.to_dict()
    missing = not d.get("ok") and str(d.get("error", "")).startswith("LookupError")
    return JSONResponse(d, status_code=404 if missing else 200)


@router.get("/carabiners")
async def list_carabiners(request: Request):
    if (refused := _gate(request)) is not None:
        return refused
    runner: KbhRunner = request.app.state.kbh
    entries: list[CarabinerEntry] = await asyncio.to_thread(runner.list)
    return {"ok": True, "node": runner.node, "carabiners": [e.to_dict() for e in entries]}


@router.get("/carabiner/{name}/settings")
async def carabiner_settings(name: str, request: Request):
    if (refused := _gate(request)) is not None:
        return refused
    return _answer(await asyncio.to_thread(request.app.state.kbh.manifest, name))


@router.post("/carabiner/{name}/settings")
async def change_carabiner_settings(name: str, request: Request, body: dict | None = None):
    if (refused := _gate(request)) is not None:
        return refused
    req = SettingsRequest.from_dict(body)
    return _answer(await asyncio.to_thread(request.app.state.kbh.apply, name, req))


@router.get("/carabiner/{name}/belay")
async def carabiner_belay(name: str, request: Request):
    if (refused := _gate(request)) is not None:
        return refused
    return _answer(await asyncio.to_thread(request.app.state.kbh.belay, name))


@router.post("/carabiner/{name}/upgrade")
async def upgrade_carabiner(name: str, request: Request, body: dict | None = None):
    """The file comes in the body; this box never fetches one. The hash is
    checked before anything of it runs (seren_sinew.carabiners)."""
    if (refused := _gate(request)) is not None:
        return refused
    req = UpgradeRequest.from_dict(body)
    return _answer(await asyncio.to_thread(request.app.state.kbh.upgrade, name, req))


@router.post("/carabiner/{name}/clip")
async def clip_carabiner(name: str, request: Request, body: dict | None = None):
    """A carabiner's first file on this box, sent by Lodestar from its shelf
    (29 Oct 2026). The hash is checked and the file written; nothing of it
    runs. One already here is an upgrade, and the answer says so."""
    if (refused := _gate(request)) is not None:
        return refused
    req = UpgradeRequest.from_dict(body)
    return _answer(await asyncio.to_thread(request.app.state.kbh.clip, name, req))


@router.post("/carabiner/{name}/unclip")
async def unclip_carabiner(name: str, request: Request, body: dict | None = None):
    """Off the Rack: the .kbh kept beside itself, the yaml left for a re-clip,
    what it registered in its harness untouched (the answer's note)."""
    if (refused := _gate(request)) is not None:
        return refused
    reason = str((body or {}).get("reason") or "") if isinstance(body, dict) else ""
    return _answer(await asyncio.to_thread(request.app.state.kbh.unclip, name, reason))
