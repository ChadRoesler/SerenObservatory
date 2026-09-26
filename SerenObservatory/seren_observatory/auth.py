"""
Bearer-token auth middleware.

Token lives in a secrets file with {"observatory_token": "..."}, chmod 600.
Written by the Starwright installer (--gen-token / --token, -GenToken / -Token)
or by hand; there is no separate secrets tool.

Where that file is (highest wins, see resolve_secrets_path):
    1. $SEREN_OBSERVATORY_SECRETS
    2. server.secrets_path in seren-observatory.yaml
    3. ~/.seren/secrets.json  (the default, and every install before this knob)
Only the LOCATION is configurable. The token itself still never goes in the
yaml - see config.py.

Skipped paths:
    /                              - root info page (links only, no service data)
    /api/v1/system/ping            - liveness probe
    /api/v1/system/version         - observatory version (no sensitive info)

Everything else requires `Authorization: Bearer <token>`.

When NO token is configured (an install without --gen-token / --token),
the observatory stays reachable for safe, read-only requests (GET/HEAD/OPTIONS) so
monitoring and bootstrap work - but it FAILS CLOSED on any state-changing
method (POST/PUT/PATCH/DELETE). This plane can restart services and trigger a
sudoers-backed reboot; an unprovisioned observatory on 0.0.0.0 must never be an open
remote-reboot button. Provision the token to unlock mutating endpoints.

Threat model: this is a Jetson on your home LAN. The token protects against
casual LAN-mate snooping and prevents drive-by RCE if you ever expose the
observatory port outside your trusted network. It is NOT designed for multi-user
or untrusted-attacker scenarios. Use a VPN or firewall if those apply.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

# The secrets file's location is resolved at CALL time, never frozen at import.
# 2026-09-25: installs are moving to per-install roots (~/seren/<install>/...)
# so two clusters on one host - each with its own Lodestar + Observatory -
# never share a token. A module constant computed at import could only ever
# name the one shared ~/.seren/secrets.json.
SECRETS_ENV = "SEREN_OBSERVATORY_SECRETS"
DEFAULT_SECRETS_PATH = "~/.seren/secrets.json"

# HTTP methods that don't change state. When no token is configured these
# stay open (read-only introspection for monitoring/bootstrap); everything
# else is refused until a token exists.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths that bypass auth. Keep this list MINIMAL - every entry here is an
# information disclosure or attack-surface concern.
PUBLIC_PATHS = frozenset({
    "/",
    "/viewer",                      # the glance HTML shell - public like /, but
                                    # its /api/v1/* fetches still carry the token,
                                    # and mutations still fail closed without one.
    "/docs",                        # the API description. The root page links it
    "/openapi.json",                # and the README promises it; the routes it
                                    # describes still need the token.
    "/api/v1/system/ping",
    "/api/v1/system/version",
})


def resolve_secrets_path(configured: str | os.PathLike[str] | None = None) -> Path:
    """$SEREN_OBSERVATORY_SECRETS -> ``configured`` (the yaml's
    server.secrets_path) -> ~/.seren/secrets.json, with ~ expanded.

    Env wins so a unit file / launcher can pin one install's secrets without
    editing its yaml. An EMPTY env value counts as unset - a blank
    ``Environment=SEREN_OBSERVATORY_SECRETS=`` line must not point the
    interlock at the working directory.
    """
    raw = os.getenv(SECRETS_ENV) or configured or DEFAULT_SECRETS_PATH
    return Path(os.path.expanduser(os.fspath(raw)))


def load_token(path: str | os.PathLike[str] | None = None) -> str | None:
    """Load the observatory token from the secrets file, or None if missing.

    ``path`` is the already-resolved secrets file (create_app passes
    resolve_secrets_path(cfg.secrets_path)); with no argument it resolves from
    the env var / default, so a bare call still honours $SEREN_OBSERVATORY_SECRETS.

    None means "auth is disabled" - the observatory will accept all requests. This
    is meant as a fallback for an install that was given no token;
    in production all installs should have a token.
    """
    secrets_path = Path(path) if path is not None else resolve_secrets_path()
    if not secrets_path.is_file():
        return None
    try:
        with open(secrets_path) as f:
            data = json.load(f)
        token = data.get("observatory_token")
        if isinstance(token, str) and token:
            return token
    except (json.JSONDecodeError, OSError):
        pass
    return None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that requires `Authorization: Bearer <token>` on
    every request EXCEPT those listed in PUBLIC_PATHS."""

    def __init__(self, app: ASGIApp, *, expected_token: str | None,
                 secrets_path: str | os.PathLike[str] | None = None) -> None:
        super().__init__(app)
        self._expected = expected_token
        # Only used to TELL the operator where the token goes. It has to be
        # the path load_token actually read, or the 503 sends them to write
        # a file the observatory will never open.
        self._secrets_path = (Path(secrets_path) if secrets_path is not None
                              else resolve_secrets_path())

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # If no token configured, stay reachable for safe reads but refuse
        # anything that changes state. The fresh-install convenience must not
        # extend to remote service restarts or a sudoers-backed reboot.
        if self._expected is None:
            if request.method not in _SAFE_METHODS:
                return JSONResponse(
                    {"error": "unauthorized",
                     "detail": "no observatory token configured; service-management "
                               f"endpoints are disabled until {self._secrets_path} "
                               "holds observatory_token (re-run the installer with "
                               "--gen-token, or write the file by hand)"},
                    status_code=503,
                )
            response = await call_next(request)
            response.headers["X-Seren-Auth"] = "disabled-no-token-configured"
            return response

        if path in PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized", "detail": "missing bearer token"},
                status_code=401,
            )

        provided = auth_header[len("Bearer "):].strip()
        # Constant-time compare to avoid timing leaks on token prefix
        if not _constant_time_eq(provided, self._expected):
            return JSONResponse(
                {"error": "unauthorized", "detail": "invalid token"},
                status_code=401,
            )

        return await call_next(request)


def _constant_time_eq(a: str, b: str) -> bool:
    """Constant-time comparison via the stdlib's audited hmac.compare_digest.

    Encodes to bytes so non-ASCII input can't raise (compare_digest rejects
    non-ASCII str). Different-length inputs return False without raising, so
    this is a drop-in for the previous hand-rolled version - and it means we
    don't pull in the `cryptography` package just to compare two tokens.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))