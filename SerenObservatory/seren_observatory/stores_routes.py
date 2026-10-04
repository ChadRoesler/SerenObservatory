"""
What a service on this node keeps, and snapshots of it - reached through the
Observatory, so the cluster head needs one address per node.

    GET  /api/v1/service/{name}/stores                        what it keeps, and its snapshots
    POST /api/v1/service/{name}/stores/snapshot               take one now  {"reason": "..."}
    GET  /api/v1/service/{name}/stores/snapshots              list them
    GET  /api/v1/service/{name}/stores/snapshots/{id}/archive one snapshot, as a tar.gz
    POST /api/v1/service/{name}/stores/snapshots/{id}/rehearse a restore's dry run of one of its own
    POST /api/v1/service/{name}/stores/rehearse               the same, of a snapshot sent as a tar.gz body
                                                              (Lodestar, from its stash)

These are the service's own routes (seren_sinew.stores - Memory and Loci
have them), proxied. The Observatory finds the service from its manifest
(port / health_url) and presents the SERVICE'S bearer, read from the config
the manifest names, by the same rules the service reads it (inline, env var,
keyring) - the way the Starwright cards' seren-mcp-headers helper does. No
token is copied anywhere; Lodestar holds the Observatory's token and nothing
else. Design note: Sinew holds the mechanism, services configure
themselves, Lodestar requests, pulls and stashes.

The Observatory's own interlock applies: a POST (take a snapshot) needs the
Observatory token like every other mutating call. A service without the store
routes answers 404, which comes back as 404 here: "this one keeps no
snapshots" is a plain answer.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from . import manifests

router = APIRouter(prefix="/api/v1/service", tags=["stores"])

TIMEOUT = 120.0            # a snapshot copies a store; an archive carries one
REHEARSE_TIMEOUT = 900.0   # a rehearsal copies a store, opens it and replays its tombstones


def service_base_url(manifest: dict[str, Any]) -> Optional[str]:
    """Where the service listens: health_url minus its path, else the port."""
    h = manifest.get("health_url")
    if h:
        u = urlsplit(str(h))
        if u.scheme and u.netloc:
            return urlunsplit((u.scheme, u.netloc, "", "", ""))
    port = manifest.get("port")
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    return f"http://127.0.0.1:{port}" if port > 0 else None


def service_bearer(manifest: dict[str, Any]) -> str:
    """The service's own bearer, from the config its manifest names, by the
    service's own rules. "" when there is none (a service on defaults, or no
    config_path in the manifest)."""
    path = manifest.get("config_path")
    if not path:
        return ""
    try:
        from seren_meninges.config import ServerConfig, read_yaml
        return ServerConfig.from_dict((read_yaml(str(path)) or {}).get("server")).resolve_bearer() or ""
    except Exception:  # noqa: BLE001 - no bearer: the service answers 401 and that is reported as such
        return ""


def _target(name: str) -> tuple[str, dict[str, str]]:
    m = manifests.load_service(name)
    if m is None:
        raise HTTPException(404, f"{name} not installed on this node")
    base = service_base_url(m)
    if not base:
        raise HTTPException(409, f"{name} has no port to reach it on")
    tok = service_bearer(m)
    return base, ({"Authorization": f"Bearer {tok}"} if tok else {})


async def _forward(name: str, method: str, path: str, body: Optional[dict] = None,
                   content: Optional[bytes] = None, timeout: float = TIMEOUT) -> httpx.Response:
    base, headers = _target(name)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if content is not None:
                return await client.request(method, base + path, content=content,
                                            headers={**headers, "Content-Type": "application/gzip"})
            return await client.request(method, base + path, headers=headers, json=body)
    except httpx.RequestError as e:
        raise HTTPException(502, f"{name} did not answer: {e}")


def _relay(name: str, r: httpx.Response, says_its_own_404: bool = False) -> Any:
    if r.status_code == 404 and not says_its_own_404:
        raise HTTPException(404, f"{name} keeps no snapshots (no store routes, or backup.enabled is off)")
    if r.status_code == 401:
        raise HTTPException(502, f"{name} refused the Observatory: its bearer could not be read from its config")
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": r.text[:300]}
    if r.status_code >= 400:
        raise HTTPException(r.status_code, (body or {}).get("error") or f"{name} answered {r.status_code}")
    return body


@router.get("/{name}/stores")
async def get_stores(name: str):
    return _relay(name, await _forward(name, "GET", "/stores"))


@router.get("/{name}/stores/snapshots")
async def get_snapshots(name: str):
    return _relay(name, await _forward(name, "GET", "/stores/snapshots"))


@router.post("/{name}/stores/snapshot")
async def post_snapshot(name: str, request: Request):
    reason = "asked through the Observatory"
    try:
        body = await request.json()
        reason = str((body or {}).get("reason") or reason)[:200]
    except Exception:  # noqa: BLE001 - no body is fine
        pass
    return _relay(name, await _forward(name, "POST", "/stores/snapshot", {"reason": reason}))


@router.get("/{name}/stores/snapshots/{snapshot_id}/archive")
async def get_archive(name: str, snapshot_id: str):
    if "/" in snapshot_id or "\\" in snapshot_id or ".." in snapshot_id:
        raise HTTPException(400, "a snapshot id has no path in it")
    r = await _forward(name, "GET", f"/stores/snapshots/{snapshot_id}/archive")
    if r.status_code != 200:
        _relay(name, r)                                     # raises with the service's reason
    return Response(content=r.content, media_type="application/gzip",
                    headers={k: v for k, v in r.headers.items()
                             if k.lower() in ("content-disposition", "x-seren-snapshot", "x-seren-service")})


@router.post("/{name}/stores/snapshots/{snapshot_id}/rehearse")
async def post_rehearse(name: str, snapshot_id: str):
    """A restore's dry run of one of the service's own snapshots. The report
    comes back as the service made it: "ok" says whether the snapshot passed."""
    if "/" in snapshot_id or "\\" in snapshot_id or ".." in snapshot_id:
        raise HTTPException(400, "a snapshot id has no path in it")
    r = await _forward(name, "POST", f"/stores/snapshots/{snapshot_id}/rehearse", timeout=REHEARSE_TIMEOUT)
    return _relay(name, r, says_its_own_404=True)


@router.post("/{name}/stores/rehearse")
async def post_rehearse_sent(name: str, request: Request):
    """A restore's dry run of a snapshot sent as a tar.gz body: how Lodestar
    proves what it has stashed can be put back on this node. Nothing is
    restored and nothing is kept; the service answers with its report."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "send the snapshot's tar.gz as the body")
    r = await _forward(name, "POST", "/stores/rehearse", content=data, timeout=REHEARSE_TIMEOUT)
    return _relay(name, r)
