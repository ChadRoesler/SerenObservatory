"""
seren-agent - main FastAPI app.

Run with:
    seren-agent                          # via the installed console script
    python -m seren_agent                # config-aware entry (--config/-c)
    python -m seren_agent.app            # directly from the source tree
    uvicorn seren_agent.app:app          # ASGI server pointing at module app

or via systemd / a launcher. Listens on 0.0.0.0:7777 by default.

Config: host/port resolve via config.load_config() (defaults < yaml's server:
block < env vars). The bearer token is loaded SEPARATELY from
~/.seren/secrets.json by auth.load_token() - it's a safety interlock, not a
config field. See config.py for why.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import __version__, manifests
from .auth import BearerAuthMiddleware, load_token
from .config import AgentConfig, load_config
from .request_log import RequestLoggingMiddleware
from .service_routes import register_all_services
from .system_routes import router as system_router

# Single source of truth for the version we report. Prefer the actually-
# installed wheel's metadata (the setuptools-scm value baked at build time);
# fall back to the package __version__ for an editable/dev checkout where the
# dist metadata may be absent or stale. This kills the old drift where app.py
# hardcoded a literal that the release process never touched.
try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    try:
        APP_VERSION = _pkg_version("seren-agent")
    except PackageNotFoundError:
        from . import __version__ as APP_VERSION
except Exception:  # noqa: BLE001 - never let version lookup break startup
    APP_VERSION = "0+unknown"


def create_app(cfg: AgentConfig | None = None) -> FastAPI:
    # cfg is accepted so the config-aware entry points (python -m seren_agent)
    # can pass a resolved config. When called with no arg (e.g. the
    # module-level `app` below, or `uvicorn seren_agent.app:app`), fall back to
    # load_config() so behaviour is identical either way. cfg currently carries
    # host/port; those are consumed by the caller that runs uvicorn, so the app
    # body doesn't need them - but we resolve it anyway so a future need (e.g.
    # surfacing the bind in the root page) has it on hand without another load.
    cfg = cfg or load_config()

    app = FastAPI(
        title="seren-agent",
        version=__version__,
        description="Per-Jetson management plane. Manifest-driven service "
                    "lifecycle, status, and orchestration. Bearer token auth "
                    "on everything except /api/v1/system/{ping,version}.",
    )

    # Request logging - wraps every request, captures timing + status +
    # 500 tracebacks. Logs go to BOTH stderr (journalctl) AND a rotating
    # file at ~/seren-logs/agent-requests.log (no sudo needed to read).
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
    app.add_middleware(RequestLoggingMiddleware)

    # Root info page - no service data, just links + auth status indicator
    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        node = manifests.load_node()
        host = (node or {}).get("hostname", "unknown")
        auth_state = "configured" if token else "DISABLED (no token in ~/.seren/secrets.json)"
        return f"""<!doctype html>
<html><head><title>seren-agent - {host}</title></head>
<body style="font-family: system-ui; max-width: 720px; margin: 2rem auto; padding: 0 1rem;">
<h1>seren-agent</h1>
<p>Per-Jetson management plane for the Seren cluster.</p>
<dl>
  <dt>Hostname:</dt> <dd>{host}</dd>
  <dt>Agent version:</dt> <dd>{__version__}</dd>
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

    # System routes (ping, version, node, services, health, reclaim)
    app.include_router(system_router)

    # Per-service routes - one router per installed service
    mounted = register_all_services(app)
    print(f"[seren-agent] mounted services: {mounted}")

    return app


# Module-level app for `uvicorn seren_agent.app:app`
app = create_app()


if __name__ == "__main__":
    import uvicorn

    # Direct `python -m seren_agent.app` path. Resolve config the same way the
    # console script does so host/port behave identically. (`python -m
    # seren_agent` -> __main__.py is the preferred, --config-aware entry.)
    _cfg = load_config()
    uvicorn.run(app, host=_cfg.host, port=_cfg.port, log_level="info")


def main() -> None:
    """Console-script entry point: `seren-agent` (declared in pyproject.toml).

    Resolves config (defaults < yaml < env) and runs uvicorn. The CLI --config
    flag is handled by __main__.py (`python -m seren_agent`); the bare
    console-script reads $SEREN_AGENT_CONFIG / the conventional path / defaults.
    """
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "seren_agent.app:app",
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        reload=False,
    )
