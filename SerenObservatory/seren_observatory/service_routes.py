"""
/api/v1/service/{name}/* - per-service operations.

Universal verbs (live on every service that's installed):
    POST   /start              - invoke ~/start_<name>.sh
    POST   /stop               - invoke ~/stop_<name>.sh
    POST   /restart            - stop, wait for the port, start
    GET    /health             - quick port probe (or library-mode short-circuit)
    GET    /status             - pid + memory + uptime + port health
    GET    /logs?lines=N       - tail of ~/seren-logs/<name>.log
    GET    /manifest           - the raw ~/.seren/services/<name>.json

Service-specific verbs (live only when a handler module exists for the
service): see seren_observatory/services/<name>.py for what each exposes.

DISCOVERED PER REQUEST, NOT AT STARTUP. The universal verbs are ONE router
with `{name}` as a path parameter, and the manifest is loaded when the
request arrives. That is what makes "drop a manifest in ~/.seren/services/
and it's live" true: the manifests docstring promised it and
/system/services delivered it, but the old code built one router per
installed service at boot, so a service installed afterwards was LISTED
and yet every lifecycle verb for it 404'd until the observatory restarted.
Lodestar saw that as "agent unreachable". Now the only thing bound at
import time is the handler modules, which are code, not installs - and each
of those loads its own manifest per request and 404s honestly if it is
absent.

Returns 404 if the service isn't installed on this node - callers can
distinguish "this node doesn't have whisper" (404) from "whisper is broken".
A start/stop that RAN and failed returns 200 with ok:false and the script's
stderr, never a 500: a 500 is what Lodestar reads as "the node did not
answer", and then the operator loses the one line that says why.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException

from . import lifecycle, manifests, services as services_pkg

router = APIRouter(prefix="/api/v1/service", tags=["service"])


def _installed(name: str) -> dict:
    m = manifests.load_service(name)
    if m is None:
        raise HTTPException(404, f"{name} not installed on this node")
    return m


@router.get("/{name}/manifest")
async def get_manifest(name: str):
    return _installed(name)


@router.post("/{name}/start")
async def post_start(name: str):
    return await lifecycle.start(_installed(name))


@router.post("/{name}/stop")
async def post_stop(name: str):
    return await lifecycle.stop(_installed(name))


@router.post("/{name}/restart")
async def post_restart(name: str):
    return await lifecycle.restart(_installed(name))


@router.get("/{name}/health")
async def get_health(name: str):
    m = _installed(name)
    # Library-mode services (coral) - no port to probe. Report installed=true
    # and let the caller decide what "healthy" means for them.
    if not manifests.service_has_port(m):
        return {"ok": True, "library_mode": True, "service": name}
    return await lifecycle.probe_port(m)


@router.get("/{name}/status")
async def get_status(name: str):
    return await lifecycle.status(_installed(name))


@router.get("/{name}/logs")
async def get_logs(name: str, lines: int = 100):
    m = _installed(name)
    if lines < 1 or lines > 10_000:
        raise HTTPException(400, "lines must be between 1 and 10000")
    return await lifecycle.tail_log(m, lines=lines)


def register_all_services(app: FastAPI) -> list[str]:
    """Mount the universal router and every service-specific handler.

    Returns the names that have handler modules. Handlers are mounted whether
    or not the service is installed right now - each one loads its manifest
    per request and 404s if it is missing - so installing a service later
    needs no restart for its specific verbs either.
    """
    app.include_router(router)
    mounted: list[str] = []
    for name in sorted(services_pkg.HANDLERS):
        specific = APIRouter(prefix=f"/api/v1/service/{name}", tags=[f"service:{name}"])
        services_pkg.HANDLERS[name](specific)
        app.include_router(specific)
        mounted.append(name)
    return mounted
