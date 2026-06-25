"""
seren-observatory - main FastAPI app.

Run with:
    seren-observatory                          # via the installed console script
    python -m seren_observatory                # config-aware entry (--config/-c)
    python -m seren_observatory.app            # directly from the source tree
    uvicorn seren_observatory.app:app          # ASGI server pointing at module app

or via systemd / a launcher. Listens on 0.0.0.0:7777 by default.

Config: host/port resolve via config.load_config() (defaults < yaml's server:
block < env vars). The bearer token is loaded SEPARATELY from
~/.seren/secrets.json by auth.load_token() - it's a safety interlock, not a
config field. See config.py for why.
"""
from __future__ import annotations

import os

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import __version__, manifests
from .auth import BearerAuthMiddleware, load_token
from .config import ObservatoryConfig, load_config
from seren_sinew.request_log import RequestLoggingMiddleware
from .service_routes import register_all_services
from .system_routes import router as system_router

from seren_meninges import get_version
from seren_meninges.viewer import render_from_dir

# Version via the shared SerenMeninges helper: the installed wheel's
# setuptools-scm metadata, falling back to the package __version__ for an
# editable/dev checkout where dist metadata may be absent. get_version never
# raises - the same one-liner the rest of the family uses, replacing the old
# hand-rolled importlib.metadata block that drifted from the others.
APP_VERSION = get_version("seren-observatory", fallback=__version__)


def create_app(cfg: ObservatoryConfig | None = None) -> FastAPI:
    # cfg is accepted so the config-aware entry points (python -m seren_observatory)
    # can pass a resolved config. When called with no arg (e.g. the
    # module-level `app` below, or `uvicorn seren_observatory.app:app`), fall back to
    # load_config() so behaviour is identical either way. cfg currently carries
    # host/port; those are consumed by the caller that runs uvicorn, so the app
    # body doesn't need them - but we resolve it anyway so a future need (e.g.
    # surfacing the bind in the root page) has it on hand without another load.
    cfg = cfg or load_config()

    app = FastAPI(
        title="seren-observatory",
        version=APP_VERSION,
        description="Per-Jetson management plane. Manifest-driven service "
                    "lifecycle, status, and orchestration. Bearer token auth "
                    "on everything except /api/v1/system/{ping,version}.",
    )

    # Request logging - wraps every request, captures timing + status +
    # 500 tracebacks. Logs go to BOTH stderr (journalctl) AND a rotating
    # file at ~/seren-logs/observatory-requests.log (no sudo needed to read).
    #
    # MUST be added BEFORE auth so we log auth-rejected requests too -
    # that's actually one of the most useful debug signals ("dashboard
    # is failing - is the bearer token wrong, or is the route 500ing?")
    #
    # Starlette/FastAPI middleware order: LIFO at request time. So adding
    # RequestLoggingMiddleware FIRST means it runs LAST on the way in
    # (closest to the route), and FIRST on the way out - wrapping the
    # entire chain. Adding auth SECOND means auth runs BEFORE logging on
    # request, which is wrong. We want logging OUTERMOST.
    #
    # Correct stack (request flow top→bottom):
    #     RequestLoggingMiddleware  (logs everything, including 401s)
    #       BearerAuthMiddleware    (rejects unauthed, logging sees the rejection)
    #         <route handler>
    #
    # FastAPI add_middleware adds in REVERSE order at runtime, so we add
    # auth FIRST (will run inner) and logging SECOND (will run outer).
    token = load_token()
    app.add_middleware(BearerAuthMiddleware, expected_token=token)
    app.add_middleware(
        RequestLoggingMiddleware,
        service_name="seren-observatory",
        env_prefix="SEREN_AGENT",
    )

    # Root info page - no service data, just links + auth status indicator
    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        node = manifests.load_node()
        host = (node or {}).get("hostname", "unknown")
        auth_state = "configured" if token else "DISABLED (no token in ~/.seren/secrets.json)"
        return f"""<!doctype html>
<html><head><title>seren-observatory - {host}</title></head>
<body style="font-family: system-ui; max-width: 720px; margin: 2rem auto; padding: 0 1rem;">
<h1>seren-observatory</h1>
<p>Per-Jetson management plane for the Seren cluster.</p>
<dl>
  <dt>Hostname:</dt> <dd>{host}</dd>
  <dt>Observatory version:</dt> <dd>{APP_VERSION}</dd>
  <dt>Auth:</dt> <dd>{auth_state}</dd>
</dl>
<h2>Endpoints</h2>
<ul>
  <li><a href="/docs">/docs</a> - interactive API docs (Swagger)</li>
  <li><a href="/api/v1/system/ping">/api/v1/system/ping</a> - public liveness</li>
  <li><a href="/api/v1/system/version">/api/v1/system/version</a> - public version</li>
  <li>/api/v1/system/{{node, services, health, reclaim}} - auth required</li>
  <li>/api/v1/service/{{name}}/{{start, stop, restart, health, status, logs, manifest}} - auth required</li>
</ul>
<p>Source of truth: ~/.seren/services/*.json + ~/.seren/node.json</p>
</body></html>"""

    # The Observatory glance - on the shared SerenMeninges baseplate.
    @app.get("/viewer", response_class=HTMLResponse)
    async def viewer() -> str:
        # Node vitals, thermals, health rollup, and the service roster with
        # lifecycle controls. PUBLIC route (the HTML shell needs no auth - see
        # auth.PUBLIC_PATHS); its /api/v1/* fetches carry the token from the
        # shell's key modal. With no token provisioned the read-only glance
        # still works (safe GETs stay open); the action buttons fail closed
        # (503) until ~/.seren/secrets.json exists - the deliberate interlock,
        # surfaced in the UI instead of hidden.
        return render_from_dir(
            Path(__file__).resolve().parent / "viewer" / "ui",
            title="seren-observatory",
            brand="Seren<b>Observatory</b>",
            subtitle=f"v{APP_VERSION} · per-node watch plane",
            accent="#6eff70",
        )

    # System routes (ping, version, node, services, health, reclaim)
    app.include_router(system_router)

    # Per-service routes - one router per installed service
    mounted = register_all_services(app)
    print(f"[seren-observatory] mounted services: {mounted}")

    return app


# Module-level app for `uvicorn seren_observatory.app:app`
app = create_app()


if __name__ == "__main__":
    import uvicorn

    # Direct `python -m seren_observatory.app` path. Resolve config the same way the
    # console script does so host/port behave identically. (`python -m
    # seren_observatory` -> __main__.py is the preferred, --config-aware entry.)
    _cfg = load_config()
    uvicorn.run(app, host=_cfg.host, port=_cfg.port, log_level="info")


def main() -> None:
    """Console-script entry point: `seren-observatory` (declared in pyproject.toml).

    Resolves config (defaults < yaml < env) and runs uvicorn. The CLI --config
    flag is handled by __main__.py (`python -m seren_observatory`); the bare
    console-script reads $SEREN_AGENT_CONFIG / the conventional path / defaults.
    """
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "seren_observatory.app:app",
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        reload=False,
    )
