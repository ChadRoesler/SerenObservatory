"""
~/.seren/{node,services/*}.json loader.

Single source of truth for "what's installed on this Jetson." Replaces
hardcoded SERVICES dict and directory probing.

The loader is read-only and cheap - call it on every request rather than
caching, so installing/wiping a service shows up in the API immediately
without restarting the agent. If we ever measure perf and this is hot,
revisit with a TTL cache.

────────────────────────────────────────────────────────────────────────
SERVICE TYPES (Path C - see lifecycle.py for handlers)

Every manifest declares a `service_type` field which dispatches lifecycle
operations to the right handler:

    pid_file        - Default. Classic ~/start_<name>.sh + PID file.
                      Used by: llama, kokoro, comfy, whisper, agent.
                      Missing field defaults here (backcompat with
                      pre-Path-C manifests).

    library         - No daemon. Code is just imported into a venv on
                      demand. port=0 always. No start/stop scripts.
                      Used by: coral. (chroma was retired - see SerenMemory)

    systemd         - Service is a systemd unit. Lifecycle = systemctl
                      start/stop/restart. Status from systemctl show.
                      Required manifest fields: systemd_unit, port (or 0).
                      Used on the NUC for: runtimehost, mcp.

    docker_compose  - Service is a container in a compose stack. Lifecycle
                      = docker compose up/down/restart <svc>. Status from
                      docker stats + docker inspect.
                      Required manifest fields: compose_file, compose_service.
                      Used on the NUC for: searxng, searxng-redis.

Manifests without `service_type` are treated as `pid_file` - keeps every
existing manifest on every Jetson working without rewrites.
────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))
MANIFEST_DIR = HOME / ".seren"
SERVICES_DIR = MANIFEST_DIR / "services"

# Bump when we add a field that older agents would mishandle. service_type
# was added at v2 - but we default missing values to "pid_file" so v1
# manifests still load cleanly. SCHEMA_VERSION is for catastrophic breaks
# only; field-level evolution stays additive.
SCHEMA_VERSION = 2

# Valid service_type values. Anything else is treated as an error at lifecycle
# dispatch time (handler returns {"ok": False, "error": "unknown service_type"}).
SERVICE_TYPES = {"pid_file", "library", "systemd", "docker_compose"}


def load_node() -> dict[str, Any] | None:
    """Load ~/.seren/node.json. Returns None if missing or schema-incompatible."""
    path = MANIFEST_DIR / "node.json"
    if not path.is_file():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("schema_version", 0) > SCHEMA_VERSION:
        return None
    return data


def load_services() -> dict[str, dict[str, Any]]:
    """Load all ~/.seren/services/*.json manifests. Returns {name: manifest}."""
    services: dict[str, dict[str, Any]] = {}
    if not SERVICES_DIR.is_dir():
        return services

    for path in sorted(SERVICES_DIR.glob("*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("schema_version", 0) > SCHEMA_VERSION:
            continue
        name = data.get("service") or path.stem
        services[name] = data

    return services


def load_service(name: str) -> dict[str, Any] | None:
    """Load a single service manifest by name. Returns None if not installed."""
    return load_services().get(name)


def service_type(manifest: dict[str, Any]) -> str:
    """Return the manifest's service_type, with backcompat default.

    Resolution order:
      1. Explicit top-level `service_type` field (Path C native)
      2. `serviceSpecific.managed_by == "systemd"` (agent's self-manifest
         uses this; written by common.sh pre-Path-C)
      3. Port-based inference:
           port == 0   → library  (coral)
           port > 0    → pid_file (llama, kokoro, comfy, whisper)

    Why the serviceSpecific.managed_by check exists: the agent's
    self-manifest pre-dates Path C. It declares port=7777 (which would
    normally infer to pid_file) but also `managed_by: systemd` in its
    serviceSpecific block. The wrapper start_script/stop_script paths
    point at shell scripts that call systemctl. Path C can manage it
    natively via the systemd handler family - no PID file, no script
    indirection, just systemctl start/stop/restart against the unit.

    Future installs that explicitly set service_type at the top level
    skip this whole resolution chain - they hit case 1 immediately.
    """
    explicit = manifest.get("service_type")
    if explicit in SERVICE_TYPES:
        return explicit

    # Agent's self-manifest (and any future pre-Path-C systemd service)
    # declares managed_by in serviceSpecific. Honor it.
    managed_by = manifest.get("serviceSpecific", {}).get("managed_by")
    if managed_by == "systemd":
        return "systemd"

    # Port-based inference: library services declare port=0, daemons
    # declare port>0. Matches the convention in common.sh.
    if manifest.get("port", 0) <= 0:
        return "library"
    return "pid_file"


def service_has_port(manifest: dict[str, Any]) -> bool:
    """True if service exposes an HTTP port (port > 0). Library-mode services    (coral) reports port=0 and are managed without HTTP probes."""
    return manifest.get("port", 0) > 0


def service_has_lifecycle(manifest: dict[str, Any]) -> bool:
    """True if the service can be started/stopped/restarted by the agent.

    Library services (no daemon) return False. PID-file, systemd, and
    docker_compose services all return True.
    """
    return service_type(manifest) != "library"