"""
~/.seren/node.json + services/*.json loader.

Where the services roster is (highest wins, see resolve_manifests_dir):
    1. $SEREN_OBSERVATORY_MANIFESTS
    2. server.manifests_dir in seren-observatory.yaml
    3. ~/.seren/services  (the default, and every install before this knob)
node.json is NOT movable: a node is one box, however many installs it holds,
so it stays at ~/.seren/node.json.

TWO ROSTERS when the install's is moved (see rosters): the install roster
above, plus the BOX roster ~/.seren/services. The box roster is where the
node installers put the GPU daemons (whisper, llama, kokoro) - they belong to
the box the way node.json does, so every install's Observatory on the host
lists them, and reclaim from any of them stops them (right: the GPU is the
box's). On a name clash the install roster wins. Each loaded manifest carries
``_roster``: "install" or "box", so /system/services says which it came from.
When both resolve to one directory it is read once, as "box".

Single source of truth for "what's installed on this NODE" — Jetson, Spark,
NUC or anything else running an Observatory. Replaces the hardcoded SERVICES
dict and directory probing.

IF A SERVICE ISN'T LISTED, IT HAS NO MANIFEST. That is the whole diagnostic.
Observatory reports these files and nothing else, so a node can be running
six healthy services and look empty. setup-seren-service.sh writes one per
install; seren-register-services.sh backfills anything installed before it
did.

The loader is read-only and cheap - call it on every request rather than
caching, so installing/wiping a service shows up in the API immediately
without restarting the observatory. If we ever measure perf and this is hot,
revisit with a TTL cache.

────────────────────────────────────────────────────────────────────────
SERVICE TYPES (Path C - see lifecycle.py for handlers)

Every manifest declares a `service_type` field which dispatches lifecycle
operations to the right handler:

    pid_file        - Default. Classic ~/start_<name>.sh + PID file.
                      Used by: llama, kokoro, comfy, whisper, observatory.
                      Missing field defaults here (backcompat with
                      pre-Path-C manifests).

    library         - No daemon. Code is just imported into a venv on
                      demand. port=0 always. No start/stop scripts.
                      Used by: coral. (chroma was retired - see SerenMemory)

    systemd         - Service is a systemd unit. Lifecycle = systemctl
                      start/stop/restart. Status from systemctl show.
                      Required manifest fields: systemd_unit, port (or 0).
                      Used on the NUC for every seren-* constellation
                      service: lodestar, workbench, memory, loci,
                      corpus-callosum, margin. (These were named
                      "runtimehost" and "mcp" before the rename; manifests
                      are written by setup-seren-service.sh, which derives
                      the unit name from the service it just installed, so
                      there is no list here to drift.)

    docker_compose  - Service is a container in a compose stack. Lifecycle
                      = docker compose up/down/restart <svc>. Status from
                      docker stats + docker inspect.
                      Required manifest fields: compose_file, compose_service.
                      Used on the NUC for: searxng, searxng-redis.

    windows_service - Service is a Windows SCM service (NSSM, installed by
                      the Starwright PowerShell cards). Lifecycle = sc.exe
                      start/stop, waiting for the state; status from sc
                      queryex. Required manifest fields: windows_service,
                      port (or 0). Written by setup-seren-service.ps1, which
                      also grants the Observatory's account start/stop/query
                      on that one service.

Manifests without `service_type` are treated as `pid_file` - keeps every
existing manifest on every Jetson working without rewrites.
────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# The roster's location is resolved at CALL time, never frozen at import - the
# same shape as auth.resolve_secrets_path. Starwright
# installs under a root (~/seren/<install>/...), and two named installs on one
# host each run an Observatory. A directory frozen at import could only ever
# name the one shared ~/.seren/services, so each would list - and restart, and
# reclaim - the other's services.
MANIFESTS_ENV = "SEREN_OBSERVATORY_MANIFESTS"
DEFAULT_MANIFESTS_DIR = "~/.seren/services"
# Per box, not per install - see the module docstring.
NODE_PATH = "~/.seren/node.json"

# The yaml's server.manifests_dir, handed over by create_app. Module state
# rather than an argument because the roster is loaded from a dozen request
# handlers (routes, reclaim, the per-service modules) that have no config in
# hand, and one Observatory process serves exactly one install.
_configured_dir: str | None = None


def configure(manifests_dir: str | os.PathLike[str] | None) -> None:
    """Record the yaml's server.manifests_dir (create_app calls this). It is
    stored raw; the env override and ~ expansion still happen per call."""
    global _configured_dir
    _configured_dir = os.fspath(manifests_dir) if manifests_dir else None


def resolve_manifests_dir(configured: str | os.PathLike[str] | None = None) -> Path:
    """$SEREN_OBSERVATORY_MANIFESTS -> ``configured`` (or the value create_app
    recorded from the yaml's server.manifests_dir) -> ~/.seren/services, with
    ~ expanded.

    Env wins so a unit file / launcher can pin one install's roster without
    editing its yaml. An EMPTY env value counts as unset - a blank
    ``Environment=SEREN_OBSERVATORY_MANIFESTS=`` line must not point the
    roster at the working directory.
    """
    raw = (os.getenv(MANIFESTS_ENV) or configured or _configured_dir
           or DEFAULT_MANIFESTS_DIR)
    return Path(os.path.expanduser(os.fspath(raw)))

# Bump when we add a field that older observatorys would mishandle. service_type
# was added at v2 - but we default missing values to "pid_file" so v1
# manifests still load cleanly. SCHEMA_VERSION is for catastrophic breaks
# only; field-level evolution stays additive.
SCHEMA_VERSION = 2

# Valid service_type values. Anything else is treated as an error at lifecycle
# dispatch time (handler returns {"ok": False, "error": "unknown service_type"}).
SERVICE_TYPES = {"pid_file", "library", "systemd", "docker_compose", "windows_service"}


def node_path() -> Path:
    """~/.seren/node.json, expanded per call like the roster (so a test's or a
    service user's HOME is the one that counts)."""
    return Path(os.path.expanduser(NODE_PATH))


def load_node() -> dict[str, Any] | None:
    """Load ~/.seren/node.json. Returns None if missing or schema-incompatible."""
    path = node_path()
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


def _same_dir(a: Path, b: Path) -> bool:
    # realpath + normcase: a trailing slash, a symlink or (on Windows) a
    # different case must not make one directory read - and listed - twice.
    return (os.path.normcase(os.path.realpath(a))
            == os.path.normcase(os.path.realpath(b)))


def rosters() -> list[tuple[str, Path]]:
    """The directories load_services reads, highest priority first, as
    (label, path). Just [("box", ~/.seren/services)] when the install roster
    is the default; [("install", <configured>), ("box", ~/.seren/services)]
    when it has been moved.

    Box-level GPU daemons are written by the node
    installers into ~/.seren/services whatever install is on the box. Moving
    an install's roster must not hide them from that install's Observatory.
    """
    install = resolve_manifests_dir()
    box = Path(os.path.expanduser(DEFAULT_MANIFESTS_DIR))
    if _same_dir(install, box):
        return [("box", box)]
    return [("install", install), ("box", box)]


def load_services() -> dict[str, dict[str, Any]]:
    """Load every *.json in every roster (see rosters). Returns {name:
    manifest}, each manifest marked with the ``_roster`` it came from; a name
    already loaded from a higher roster is not replaced by a lower one."""
    services: dict[str, dict[str, Any]] = {}
    for label, services_dir in rosters():
        if not services_dir.is_dir():
            continue
        for path in sorted(services_dir.glob("*.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("schema_version", 0) > SCHEMA_VERSION:
                continue
            name = data.get("service") or path.stem
            if name in services:
                continue
            # Read-only marker: nothing writes a manifest back to disk, and
            # Lodestar's DTOs drop keys they don't know.
            data["_roster"] = label
            services[name] = data

    return services


def load_service(name: str) -> dict[str, Any] | None:
    """Load a single service manifest by name. Returns None if not installed."""
    return load_services().get(name)


def service_type(manifest: dict[str, Any]) -> str:
    """Return the manifest's service_type, with backcompat default.

    Resolution order:
      1. Explicit top-level `service_type` field (Path C native)
      2. `serviceSpecific.managed_by == "systemd"` (observatory's self-manifest
         uses this; written by common.sh pre-Path-C)
      3. Port-based inference:
           port == 0   → library  (coral)
           port > 0    → pid_file (llama, kokoro, comfy, whisper)

    Why the serviceSpecific.managed_by check exists: the observatory's
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

    # Observatory's self-manifest (and any future pre-Path-C systemd service)
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
    """True if the service can be started/stopped/restarted by the observatory.

    Library services (no daemon) return False. PID-file, systemd, and
    docker_compose services all return True.
    """
    return service_type(manifest) != "library"

# ── orchestrated: off on purpose ─────────────────────────────────────────────
# A service can be flagged 'orchestrated' so it does not report as unhealthy
# when it is healthy and simply not on because of orchestration. llama, kokoro, whisper and comfy are
# started by Lodestar when someone needs them and stopped when nobody does
# (seren_sinew.orchestration). Between uses they are OFF, and off was read as
# not_running, which made the node degraded and its health pill red for doing
# exactly what it was built to do.
#
# The flag lives in the manifest. It is set three ways: the node installer
# writes it for the on-demand services; POST /service/{name}/orchestrated
# flips it by hand; and an `ensure` that STARTED a service sets it, because
# that is the proof. This is the one key anything writes back into a
# manifest; everything else in a manifest is the installer's.

def is_orchestrated(manifest: dict[str, Any]) -> bool:
    v = manifest.get("orchestrated")
    return v is True or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on"))


def manifest_path(name: str) -> Path | None:
    """The file a service's manifest was loaded from (the roster that won)."""
    for _label, services_dir in rosters():
        p = services_dir / f"{name}.json"
        if p.is_file():
            return p
    return None


def set_orchestrated(name: str, flag: bool) -> dict[str, Any]:
    """Write `orchestrated` into the service's manifest file and return the
    manifest as it now reads. Atomic (tmp + replace); every other key kept.
    Raises FileNotFoundError when there is no such service."""
    path = manifest_path(name)
    if path is None:
        raise FileNotFoundError(name)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["orchestrated"] = bool(flag)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return data
