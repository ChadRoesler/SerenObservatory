"""
/api/v1/service/{name}/* - per-service operations.

Universal verbs (live on every service that's installed):
    POST   /start              - invoke ~/start_<name>.sh
    POST   /stop               - invoke ~/stop_<name>.sh
    POST   /restart            - stop, brief pause, start
    GET    /health             - quick port probe (or library-mode short-circuit)
    GET    /status             - pid + memory + uptime + port health
    GET    /logs?lines=N       - tail of ~/seren-logs/<name>.log
    GET    /manifest           - the raw ~/.seren/services/<name>.json

Service-specific verbs (live only when the service module supplies them):
    See agent/services/<name>.py for what each service exposes.

Returns 404 if the service isn't installed on this node - callers can
distinguish "this Jetson doesn't have whisper" (404) from "whisper is
broken" (500). Per the user's design rule: if the service isn't firing,
ignore it.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException

from . import lifecycle, manifests, services as services_pkg


def build_service_router(name: str) -> APIRouter:
    """Build the router for a specific service. Mounted at
    /api/v1/service/{name}.

    Each service gets its own router instance so we can attach
    service-specific endpoints without colliding."""
    router = APIRouter(prefix=f"/api/v1/service/{name}", tags=[f"service:{name}"])

    @router.get("/manifest")
    async def get_manifest():
        m = manifests.load_service(name)
        if m is None:
            raise HTTPException(404, f"{name} not installed on this node")
        return m

    @router.post("/start")
    async def post_start():
        m = manifests.load_service(name)
        if m is None:
            raise HTTPException(404, f"{name} not installed on this node")
        result = lifecycle.start(m)
        if not result.get("ok") and "error" in result:
            raise HTTPException(500, result["error"])
        return result

    @router.post("/stop")
    async def post_stop():
        m = manifests.load_service(name)
        if m is None:
            raise HTTPException(404, f"{name} not installed on this node")
        result = lifecycle.stop(m)
        if not result.get("ok") and "error" in result:
            raise HTTPException(500, result["error"])
        return result

    @router.post("/restart")
    async def post_restart():
        m = manifests.load_service(name)
        if m is None:
            raise HTTPException(404, f"{name} not installed on this node")
        return lifecycle.restart(m)

    @router.get("/health")
    async def get_health():
        m = manifests.load_service(name)
        if m is None:
            raise HTTPException(404, f"{name} not installed on this node")

        # Library-mode services (coral) - no port to probe.
        # Report installed=true and let the caller decide what "healthy"
        # means for them.
        if not manifests.service_has_port(m):
            return {"ok": True, "library_mode": True, "service": name}

        return await lifecycle.probe_port(m)

    @router.get("/status")
    async def get_status():
        m = manifests.load_service(name)
        if m is None:
            raise HTTPException(404, f"{name} not installed on this node")
        return await lifecycle.status(m)

    @router.get("/logs")
    async def get_logs(lines: int = 100):
        m = manifests.load_service(name)
        if m is None:
            raise HTTPException(404, f"{name} not installed on this node")
        if lines < 1 or lines > 10_000:
            raise HTTPException(400, "lines must be between 1 and 10000")
        return lifecycle.tail_log(m, lines=lines)

    # Mount service-specific endpoints (voices, models, checkpoints, etc).
    # If no module exists for this service, only the universal verbs above
    # are available.
    handler = services_pkg.get_handler(name)
    if handler is not None:
        handler(router)

    return router


def register_all_services(app: FastAPI) -> list[str]:
    """Walk ~/.seren/services/ and mount a router per installed service.

    Returns the list of service names mounted. Called once at startup;
    if you install a new service the agent must be restarted to pick it
    up. (Manifests themselves are read fresh on every request, but the
    Python handler modules and routers are bound at import time.)
    """
    installed = manifests.load_services()
    mounted = []
    for name in sorted(installed):
        router = build_service_router(name)
        app.include_router(router)
        mounted.append(name)
    return mounted
