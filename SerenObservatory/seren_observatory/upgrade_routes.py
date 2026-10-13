"""
Upgrades of this node's services (seren_observatory.upgrades).

    POST /api/v1/service/{name}/upgrade   start one; 202 with the job
        {"version": "5.0.1" | "latest" | null,     null/latest = the newest the index has
         "with": ["seren-sinew", ...],             moved alongside (default: sinew + meninges)
         "snapshot": true,                         the service's own snapshot first (Lodestar,
                                                   having pulled one off the box, sends false)
         "dry_run": false,                         true = pip's own plan, nothing changed, 200
         "wait_seconds": 90,                       how long a restart may take to answer health
         "peers": [{"service", "node",             what the cluster runs, for the peer check
                    "package", "version"}],        (Lodestar sends it; default: this node's own)
         "reason": "..."}
    GET  /api/v1/upgrades                 the recent jobs, newest first
    GET  /api/v1/upgrades/versions        every service here an upgrade can see: package,
                                          version, and the peers it needs
    GET  /api/v1/upgrades/{id}            one job: its state, every step, pip's last lines

Everything a request names ends up on a pip command line, so it is checked
first: a version is a version, and a package moved alongside is a seren-*
distribution, never a flag or a URL. POSTs need the Observatory token like
every other mutating call.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import manifests, upgrades

router = APIRouter(prefix="/api/v1", tags=["upgrades"])

VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+!]*$")
WITH_RE = re.compile(r"^seren-[a-z0-9][a-z0-9._-]*((==|>=|<=|~=|<|>)[0-9][0-9A-Za-z.*+!]*)?$")


def _upgrader(request: Request) -> upgrades.Upgrader:
    up = getattr(request.app.state, "upgrader", None)
    if up is None:
        up = request.app.state.upgrader = upgrades.Upgrader()
    return up


MAX_PEERS = 500


def _peer_ok(x: Any) -> bool:
    return (isinstance(x, dict) and all(isinstance(x.get(k), str) and 0 < len(x[k]) <= 200
                                        for k in ("service", "package"))
            and all(x.get(k) is None or (isinstance(x[k], str) and len(x[k]) <= 200) for k in ("node", "version")))


def parse_request(body: Any) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    version = body.get("version")
    if version in (None, "", "latest"):
        version = None
    elif not isinstance(version, str) or not VERSION_RE.match(version.strip()):
        raise HTTPException(400, "version must be a version (5.0.1) or 'latest'")
    else:
        version = version.strip()
    with_ = body.get("with", list(upgrades.DEFAULT_WITH))
    if with_ is None:
        with_ = []
    if not isinstance(with_, list) or not all(isinstance(w, str) and WITH_RE.match(w.strip()) for w in with_):
        raise HTTPException(400, "with must be a list of seren-* distributions, optionally pinned (seren-sinew==5.0.0)")
    try:
        wait = float(body.get("wait_seconds", upgrades.HEALTH_WAIT_S))
    except (TypeError, ValueError):
        raise HTTPException(400, "wait_seconds must be a number")
    peers = body.get("peers")
    if peers is not None:
        if not (isinstance(peers, list) and len(peers) <= MAX_PEERS and all(_peer_ok(x) for x in peers)):
            raise HTTPException(400, "peers must be a list of {service, node, package, version}")
        peers = [{k: x.get(k) for k in ("service", "node", "package", "version")} for x in peers]
    return {"version": version, "with_": [w.strip() for w in with_], "peers": peers,
            "snapshot": bool(body.get("snapshot", True)), "dry_run": bool(body.get("dry_run", False)),
            "wait_seconds": min(max(wait, 5.0), 900.0),
            "reason": str(body.get("reason") or "by hand")[:200]}


@router.post("/service/{name}/upgrade")
async def post_upgrade(name: str, request: Request):
    m = manifests.load_service(name)
    if m is None:
        raise HTTPException(404, f"{name} not installed on this node")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - no body: the newest version, the defaults
        body = None
    req = parse_request(body)
    up = _upgrader(request)
    if req["dry_run"]:
        return await up.plan(name, m, req["version"], req["with_"], peers=req["peers"])
    status, out = up.start(name, m, req["version"], req["with_"], req["snapshot"], req["reason"],
                           wait_seconds=req["wait_seconds"], peers=req["peers"])
    return JSONResponse(out, status_code=status)


@router.get("/upgrades")
async def get_upgrades(request: Request, limit: int = 20):
    up = _upgrader(request)
    return {"ok": True, "busy": up.busy,
            "current": up.current.as_dict() if up.busy and up.current else None,
            "jobs": [j.as_dict() for j in upgrades.recent(max(1, min(limit, upgrades.KEEP_JOBS)))]}


@router.get("/upgrades/versions")
async def get_versions():
    """Every service on this node an upgrade can see, with its version and
    the peers it needs: what Lodestar collects to check the cluster."""
    import asyncio
    return {"ok": True, "services": await asyncio.to_thread(upgrades.node_versions)}


@router.get("/upgrades/{job_id}")
async def get_upgrade(job_id: str):
    job = upgrades.load(job_id)
    if job is None:
        raise HTTPException(404, f"no upgrade job {job_id}")
    return {"ok": True, "job": job.as_dict()}
