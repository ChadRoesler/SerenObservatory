"""
Service lifecycle helpers - uniform operations across all service types.

────────────────────────────────────────────────────────────────────────
Path C dispatcher: service_type drives which handler does the work.

    pid_file        → _pid_*  family. Classic ~/start_<name>.sh + PID file.
    library         → _library_* family. No daemon, no lifecycle ops.
    systemd         → _systemd_* family. systemctl-managed unit.
    docker_compose  → _docker_* family. compose stack containers.

The public functions (start, stop, restart, status, tail_log) read the
service_type from the manifest and dispatch. Adding a new type means
adding a new handler family + one branch in the dispatcher; no caller
ever needs to know which type a service is.

Each handler returns the same shape, so the API surface above doesn't
care:
    start/stop/restart → {ok, error?, pid?, exit_code?, stdout?, stderr?, ...}
    status             → {service, service_type, running, pid?, memory_mb?,
                          cpu_percent?, uptime_seconds?, port_health?, ...}
    tail_log           → {ok, lines?, log_path?, error?}
────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import asyncio
from collections import deque
from pathlib import Path
from typing import Any

import httpx

from . import manifests


# ═════════════════════════════════════════════════════════════════════
# Shared low-level helpers - process info, port probes, log tailing.
# Used by multiple handler families.
# ═════════════════════════════════════════════════════════════════════

def proc_rss_mb(pid: int) -> int | None:
    """Resident Set Size in MB for a given PID. None if /proc unavailable."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def proc_uptime_seconds(pid: int) -> int | None:
    """How long the process has been running, in seconds."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        rparen = next(i for i, f in enumerate(fields) if f.endswith(")"))
        starttime_ticks = int(fields[rparen + 20])
        with open("/proc/uptime") as f:
            sys_uptime = float(f.read().split()[0])
        clk_tck = os.sysconf("SC_CLK_TCK")
        proc_start_secs = starttime_ticks / clk_tck
        return int(sys_uptime - proc_start_secs)
    except (OSError, ValueError, StopIteration, IndexError):
        return None


def _read_proc_cpu_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        rparen = next(i for i, f in enumerate(fields) if f.endswith(")"))
        utime = int(fields[rparen + 13])
        stime = int(fields[rparen + 14])
        return utime + stime
    except (OSError, ValueError, StopIteration, IndexError):
        return None


def _read_total_cpu_ticks() -> int | None:
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()
        if parts[0] != "cpu":
            return None
        return sum(int(x) for x in parts[1:8])
    except (OSError, ValueError, IndexError):
        return None


async def proc_cpu_percent(pid: int, sample_ms: int = 100) -> float | None:
    """CPU usage as percent of one core, sampled over `sample_ms`.

    Two-sample delta. 100% = one full core saturated. Blocks for sample_ms.
    None on any /proc read failure (PID gone, permissions, etc.).
    """
    p1 = _read_proc_cpu_ticks(pid)
    s1 = _read_total_cpu_ticks()
    if p1 is None or s1 is None:
        return None
    await asyncio.sleep(sample_ms / 1000.0)
    p2 = _read_proc_cpu_ticks(pid)
    s2 = _read_total_cpu_ticks()
    if p2 is None or s2 is None:
        return None
    sys_delta = s2 - s1
    if sys_delta <= 0:
        return None
    proc_delta = p2 - p1
    n_cores = os.cpu_count() or 1
    return round((proc_delta / sys_delta) * 100.0 * n_cores, 1)


async def _http_probe(url: str, timeout: float = 2.0) -> dict[str, Any]:
    """Generic HTTP GET probe. Shape matches port_health response."""
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": 200 <= response.status_code < 500,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
        }
    except httpx.RequestError as e:
        return {"ok": False, "error": str(e)}


def _wait_for_port_release(port: int, timeout_s: float = 10.0) -> bool:
    """Poll until the port is unbound, or timeout. Used by restart()."""
    if port <= 0:
        return True
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(0.25)
    return False


def _tail_file(path: str | None, lines: int) -> dict[str, Any]:
    """Generic file-tail. Returns {ok, lines, log_path} or {ok, error}."""
    if not path:
        return {"ok": False, "error": "no log_path in manifest"}
    p = Path(path)
    if not p.is_file():
        return {"ok": True, "lines": [], "log_path": path,
                "note": "log file does not exist yet"}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max(1, min(lines, 10_000)))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "lines": [line.rstrip("\n") for line in tail],
        "log_path": path,
    }


# ═════════════════════════════════════════════════════════════════════
# PID_FILE handler family - classic ~/start_<name>.sh + PID file pattern
# ═════════════════════════════════════════════════════════════════════

def _pid_read(manifest: dict[str, Any]) -> int | None:
    """PID from the service's PID file, or None if not running.

    Falls back to the standard ~/seren-logs/<name>.pid path if the manifest
    doesn't explicitly declare pid_path. Same convention as the start/stop
    script fallbacks - most service manifests rely on the implicit pattern
    rather than declaring every path explicitly.
    """
    pid_path = manifest.get("pid_path")
    if not pid_path:
        name = manifest.get("service")
        if name:
            pid_path = f"{Path.home()}/seren-logs/{name}.pid"
    if not pid_path or not Path(pid_path).is_file():
        return None
    try:
        with open(pid_path) as f:
            text = f.read().strip()
        if not text:
            return None
        pid = int(text)
        os.kill(pid, 0)  # verify alive; stale PID files happen
        return pid
    except (OSError, ValueError):
        return None


def _pid_start(manifest: dict[str, Any]) -> dict[str, Any]:
    # Conventional fallback: ~/start_<name>.sh if the manifest doesn't
    # name an explicit path. This matches common.sh's convention - most
    # services (llama, kokoro, comfy) don't list start_script in their
    # manifest because the path is implicit. Only whisper happens to be
    # explicit. Either way works.
    start_script = manifest.get("start_script")
    if not start_script:
        name = manifest.get("service")
        if name:
            start_script = f"{Path.home()}/start_{name}.sh"

    if not start_script:
        return {"ok": False, "error": "service has no start_script and no resolvable name"}

    if _pid_read(manifest) is not None:
        return {"ok": True, "already_running": True, "pid": _pid_read(manifest)}

    if not Path(start_script).is_file():
        return {"ok": False, "error": f"start script not found: {start_script}"}

    try:
        result = subprocess.run(
            ["bash", start_script],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "start script timed out after 30s"}

    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "pid": _pid_read(manifest),
    }


def _pid_stop(manifest: dict[str, Any]) -> dict[str, Any]:
    was_running = _pid_read(manifest) is not None
    if not was_running:
        return {"ok": True, "was_running": False}

    # Same conventional fallback as _pid_start
    stop_script = manifest.get("stop_script")
    if not stop_script:
        name = manifest.get("service")
        if name:
            stop_script = f"{Path.home()}/stop_{name}.sh"

    if not stop_script:
        return {"ok": False, "error": "service has no stop_script and no resolvable name"}
    if not Path(stop_script).is_file():
        return {"ok": False, "error": f"stop script not found: {stop_script}"}

    try:
        result = subprocess.run(
            ["bash", stop_script],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "stop script timed out after 30s"}

    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "was_running": was_running,
    }


async def _pid_port_health(manifest: dict[str, Any]) -> dict[str, Any]:
    """HTTP-probe the service's port. Tries /health, falls back to /."""
    port = manifest.get("port", 0)
    if port <= 0:
        return {"ok": False, "reason": "library-mode (no port)"}

    last_err = None
    for path in ("/health", "/"):
        result = await _http_probe(f"http://127.0.0.1:{port}{path}")
        if result.get("ok"):
            return {**result, "probed_path": path}
        last_err = result.get("error", "non-2xx response")
    return {"ok": False, "error": last_err}


async def _pid_status(manifest: dict[str, Any]) -> dict[str, Any]:
    pid = _pid_read(manifest)
    if pid is None:
        return {
            "service": manifest.get("service"),
            "service_type": "pid_file",
            "running": False,
        }
    rss = proc_rss_mb(pid)
    uptime = proc_uptime_seconds(pid)
    cpu = await proc_cpu_percent(pid)
    port_health = await _pid_port_health(manifest) if manifests.service_has_port(manifest) else None
    return {
        "service": manifest.get("service"),
        "service_type": "pid_file",
        "running": True,
        "pid": pid,
        "memory_mb": rss,
        "cpu_percent": cpu,
        "uptime_seconds": uptime,
        "port_health": port_health,
    }


# ═════════════════════════════════════════════════════════════════════
# LIBRARY handler family - code imported on demand, no daemon
# ═════════════════════════════════════════════════════════════════════

async def _library_status(manifest: dict[str, Any]) -> dict[str, Any]:
    """Library mode: manifest existing means the capability is available.
    No daemon, no PID, no port, nothing to start or stop."""
    return {
        "service": manifest.get("service"),
        "service_type": "library",
        "running": True,
        "library_mode": True,
    }


def _library_unsupported_op(op: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"{op} not supported for library-mode services",
        "service_type": "library",
    }


# ═════════════════════════════════════════════════════════════════════
# SYSTEMD handler family - services that are systemd units
# ═════════════════════════════════════════════════════════════════════

def _systemd_unit_name(manifest: dict[str, Any]) -> str | None:
    """Find the systemd unit name in the manifest.

    Modern Path-C manifests put it at the top level: manifest["systemd_unit"].
    The agent's pre-Path-C self-manifest puts it nested at
    manifest["serviceSpecific"]["systemd_unit"]. Honor both so legacy
    manifests work without rewriting.
    """
    explicit = manifest.get("systemd_unit")
    if explicit:
        return explicit
    return manifest.get("serviceSpecific", {}).get("systemd_unit")


def _systemd_run(args: list[str], timeout: float = 10.0) -> dict[str, Any]:
    """Run a systemctl command via sudo -n.

    -n = non-interactive: fail fast if sudoers isn't configured rather
    than hanging on a password prompt.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"systemctl timed out after {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "error": "systemctl not on PATH"}

    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _systemd_is_active(unit: str) -> bool:
    """True iff `systemctl is-active <unit>` exits 0. Doesn't require sudo."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _systemd_show(unit: str, properties: list[str]) -> dict[str, str]:
    """Get specific systemd unit properties. {property: value}."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", ",".join(properties), unit],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _systemd_start(manifest: dict[str, Any]) -> dict[str, Any]:
    unit = _systemd_unit_name(manifest)
    if not unit:
        return {"ok": False, "error": "manifest missing systemd_unit"}
    if _systemd_is_active(unit):
        return {"ok": True, "already_running": True}
    return _systemd_run(["systemctl", "start", unit])


def _systemd_stop(manifest: dict[str, Any]) -> dict[str, Any]:
    unit = _systemd_unit_name(manifest)
    if not unit:
        return {"ok": False, "error": "manifest missing systemd_unit"}
    was_running = _systemd_is_active(unit)
    if not was_running:
        return {"ok": True, "was_running": False}
    result = _systemd_run(["systemctl", "stop", unit])
    result["was_running"] = was_running
    return result


def _systemd_restart(manifest: dict[str, Any]) -> dict[str, Any]:
    """systemctl restart handles port release internally - it waits for
    the unit to be inactive before starting the new one. No need for
    the port-poll dance we do for pid_file services.
    """
    unit = _systemd_unit_name(manifest)
    if not unit:
        return {"ok": False, "error": "manifest missing systemd_unit"}
    return _systemd_run(["systemctl", "restart", unit])


async def _systemd_status(manifest: dict[str, Any]) -> dict[str, Any]:
    unit = _systemd_unit_name(manifest)
    if not unit:
        return {
            "service": manifest.get("service"),
            "service_type": "systemd",
            "running": False,
            "error": "manifest missing systemd_unit",
        }

    if not _systemd_is_active(unit):
        return {
            "service": manifest.get("service"),
            "service_type": "systemd",
            "running": False,
            "systemd_unit": unit,
        }

    # systemctl show gives us authoritative state. MemoryCurrent reflects
    # the cgroup's memory which is accurate even for services that fork
    # children. MainPID gives us a handle into /proc for CPU% + uptime.
    props = _systemd_show(unit, ["MainPID", "MemoryCurrent"])

    main_pid_str = props.get("MainPID", "0")
    try:
        main_pid = int(main_pid_str)
    except ValueError:
        main_pid = 0

    mem_bytes_str = props.get("MemoryCurrent", "")
    try:
        memory_mb: int | None = int(mem_bytes_str) // (1024 * 1024)
    except ValueError:
        # MemoryCurrent comes back as "[not set]" when cgroup accounting
        # is off; fall back to RSS of the main process.
        memory_mb = proc_rss_mb(main_pid) if main_pid > 0 else None

    cpu = await proc_cpu_percent(main_pid) if main_pid > 0 else None
    uptime = proc_uptime_seconds(main_pid) if main_pid > 0 else None

    port_health = None
    if manifests.service_has_port(manifest):
        # systemd services can declare a non-default health_url in the
        # manifest. Falls back to the pid_file-style /health probe if not.
        health_url = manifest.get("health_url")
        if health_url:
            port_health = await _http_probe(health_url)
        else:
            port_health = await _pid_port_health(manifest)

    return {
        "service": manifest.get("service"),
        "service_type": "systemd",
        "running": True,
        "pid": main_pid if main_pid > 0 else None,
        "memory_mb": memory_mb,
        "cpu_percent": cpu,
        "uptime_seconds": uptime,
        "port_health": port_health,
        "systemd_unit": unit,
    }


# ═════════════════════════════════════════════════════════════════════
# DOCKER_COMPOSE handler family - containers in a compose stack
# ═════════════════════════════════════════════════════════════════════
#
# Performance shape: docker shellouts are SLOW. `docker stats --no-stream`
# is ~1-2s per call because it has to sample CPU stats twice with a small
# delay between samples. `docker compose ps --format json` is ~200ms
# because compose re-parses the YAML each invocation.
#
# Naive implementation called both shellouts per-container, per status
# request - N containers → 2N shellouts → ~4s for a 2-container stack.
# That blew past RuntimeHost's polling timeout and made the NUC look
# unreachable.
#
# Fix: ONE shellout for the entire host's docker state, cached for
# `_DOCKER_CACHE_TTL_S` seconds. With searxng + redis on the same host,
# a polling cycle now does:
#   - 1 × docker ps (~50ms)
#   - 1 × docker stats (~1.5s)  ← still slow but only once per cycle
# = ~1.5s total for the whole compose stack, regardless of how many
# containers. Per-container status calls then just look up the cached
# data in O(1).
#
# Plus: the blocking subprocess.run calls are wrapped in asyncio.to_thread
# in the async status path so the event loop doesn't stall on docker IO.
# ═════════════════════════════════════════════════════════════════════

_DOCKER_CACHE_TTL_S = 2.0  # short enough to feel live, long enough to dedupe
_docker_cache: dict[str, Any] = {
    "states_at": 0.0,    # monotonic timestamp of last states fetch
    "states": {},        # {container_name: state_obj}
    "stats_at": 0.0,
    "stats": {},         # {container_id_short: {memory_mb, cpu_percent}}
    "lock": None,        # asyncio.Lock created lazily (event-loop-bound)
}


def _docker_compose_args(manifest: dict[str, Any]) -> list[str] | None:
    compose_file = manifest.get("compose_file")
    if not compose_file:
        return None
    return ["docker", "compose", "-f", compose_file]


def _docker_compose_service_name(manifest: dict[str, Any]) -> str | None:
    """Which service in the compose stack this manifest manages."""
    return manifest.get("compose_service") or manifest.get("service")


def _docker_run(args: list[str], timeout: float = 30.0) -> dict[str, Any]:
    """Run a docker command via sudo -n. BLOCKING - async callers should
    wrap in asyncio.to_thread for non-blocking behavior.

    We use sudo because the agent may not have live docker group membership
    in its systemd session - group changes from `usermod -aG docker` only
    take effect on next login. Sudoers grant for /usr/bin/docker is added
    by host-tooling-setup.sh.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"docker timed out after {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "error": "docker not on PATH"}

    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _docker_ps_all() -> dict[str, dict[str, Any]]:
    """One-shot fetch of all containers' state via `docker ps -a --format json`.

    Returns {container_name: state_obj}. Container name matches the compose
    'container_name' field (or compose's auto-generated name).

    Includes stopped containers (`-a`) so we can correctly report a stopped
    container as `running: False` rather than missing-from-cluster.
    """
    result = _docker_run(
        ["docker", "ps", "-a", "--format", "json", "--no-trunc"],
        timeout=8.0,
    )
    if not result["ok"]:
        return {}

    states: dict[str, dict[str, Any]] = {}
    for line in result["stdout"].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # `docker ps` field names:
        #   Names="seren-searxng", State="running", ID="..."
        name = obj.get("Names") or obj.get("Name")
        if not name:
            continue
        primary = name.split(",")[0].strip()
        states[primary] = {
            "ID": obj.get("ID") or obj.get("Id"),
            "Name": primary,
            "State": obj.get("State"),
            "Status": obj.get("Status"),  # e.g. "Up 2 hours"
            "Image": obj.get("Image"),
            "CreatedAt": obj.get("CreatedAt"),
        }
    return states


def _parse_docker_size_to_mb(s: str) -> int | None:
    """Convert docker's '12.3MiB', '1.5GiB' etc to MB."""
    s = s.strip()
    if not s:
        return None
    for suffix, multiplier in [
        ("TiB", 1024 * 1024), ("GiB", 1024), ("MiB", 1),
        ("KiB", 1 / 1024), ("B", 1 / (1024 * 1024)),
    ]:
        if s.endswith(suffix):
            try:
                return int(float(s[:-len(suffix)]) * multiplier)
            except ValueError:
                return None
    return None


def _docker_stats_all() -> dict[str, dict[str, Any]]:
    """One-shot stats for ALL running containers.

    `docker stats --no-stream` without arguments returns one row per
    running container. ~1.5s for the whole host regardless of how many
    containers - same cost as a single-container query because the
    bottleneck is the CPU sampling delay, not the per-container work.

    Returns {container_id_short: {memory_mb, cpu_percent}}.
    """
    result = _docker_run(
        ["docker", "stats", "--no-stream", "--format",
         "{{.ID}}|{{.MemUsage}}|{{.CPUPerc}}"],
        timeout=10.0,
    )
    if not result["ok"]:
        return {}

    stats: dict[str, dict[str, Any]] = {}
    for line in result["stdout"].splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        cid, mem_usage_str, cpu_str = parts

        memory_mb: int | None = None
        try:
            used_str = mem_usage_str.split("/")[0].strip()
            memory_mb = _parse_docker_size_to_mb(used_str)
        except (ValueError, IndexError):
            pass

        cpu_percent: float | None = None
        try:
            cpu_percent = float(cpu_str.rstrip("%").strip())
        except ValueError:
            pass

        # docker stats returns the SHORT ID (12 chars) by default.
        stats[cid.strip()] = {
            "memory_mb": memory_mb,
            "cpu_percent": cpu_percent,
        }
    return stats


async def _docker_refresh_cache_if_stale() -> None:
    """Refresh the module-level docker cache if older than TTL.

    Uses an asyncio.Lock so concurrent status calls don't all race to
    refresh - first caller refreshes, others wait briefly and then read
    the freshened cache. The blocking subprocess work happens in a
    worker thread so the event loop stays responsive.
    """
    import asyncio as _asyncio

    if _docker_cache["lock"] is None:
        _docker_cache["lock"] = _asyncio.Lock()

    now = time.monotonic()
    states_age = now - _docker_cache["states_at"]
    stats_age = now - _docker_cache["stats_at"]
    if states_age < _DOCKER_CACHE_TTL_S and stats_age < _DOCKER_CACHE_TTL_S:
        return  # cache fresh enough, nothing to do

    async with _docker_cache["lock"]:
        # Re-check inside the lock - another coroutine may have refreshed
        # while we were waiting.
        now = time.monotonic()
        states_age = now - _docker_cache["states_at"]
        stats_age = now - _docker_cache["stats_at"]
        if states_age >= _DOCKER_CACHE_TTL_S:
            states = await _asyncio.to_thread(_docker_ps_all)
            _docker_cache["states"] = states
            _docker_cache["states_at"] = time.monotonic()
        if stats_age >= _DOCKER_CACHE_TTL_S:
            stats = await _asyncio.to_thread(_docker_stats_all)
            _docker_cache["stats"] = stats
            _docker_cache["stats_at"] = time.monotonic()


def _docker_container_state(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Read this container's state from the cached snapshot.

    SYNCHRONOUS - used by start/stop/restart which run sync. Falls back
    to a live shellout if the cache is cold (first call after agent
    startup). Subsequent calls within TTL hit the cache.

    Resolution strategy for container name:
      1. The manifest's compose_service field tells us the compose stack
         logical name. Compose builds container names like
         "seren-searxng" (matching the container_name: in docker-compose.yml)
         OR "<project>-<service>-N" (auto-generated when container_name is
         absent).
      2. We search the cached states for either an exact match OR an
         endswith match on the service name.
    """
    svc = _docker_compose_service_name(manifest)
    if not svc:
        return None

    states = _docker_cache.get("states") or {}
    if not states:
        # Cold cache - do a sync fetch. This blocks but only happens once.
        states = _docker_ps_all()
        _docker_cache["states"] = states
        _docker_cache["states_at"] = time.monotonic()

    # Exact name match first
    for name, state in states.items():
        if name == svc:
            return state
    # Fallback: container_name from compose. Typical patterns are
    # "seren-<svc>" or "<svc>". Match by suffix.
    for name, state in states.items():
        if name.endswith(f"-{svc}") or name.endswith(f"_{svc}"):
            return state
    return None


def _docker_stats(container_id: str | None) -> dict[str, Any] | None:
    """Lookup pre-fetched stats for a container ID. Short ID match.

    Called from the sync path; reads the module cache only (no blocking
    shellout). Returns None if container not in cache or not running.
    """
    if not container_id:
        return None
    stats = _docker_cache.get("stats") or {}
    # Container IDs can be long (64 char) or short (12 char). docker stats
    # uses short by default. Match by prefix on either form.
    short = container_id[:12]
    if short in stats:
        return stats[short]
    # Defensive: scan if direct lookup missed (e.g. cache has long IDs)
    for cid, s in stats.items():
        if cid.startswith(short) or short.startswith(cid):
            return s
    return None


def _docker_start(manifest: dict[str, Any]) -> dict[str, Any]:
    compose = _docker_compose_args(manifest)
    svc = _docker_compose_service_name(manifest)
    if not compose or not svc:
        return {"ok": False, "error": "manifest missing compose_file or compose_service"}

    state = _docker_container_state(manifest)
    if state and state.get("State") == "running":
        return {"ok": True, "already_running": True}
    result = _docker_run(compose + ["up", "-d", svc], timeout=120.0)
    # Invalidate cache - state's about to change.
    _docker_cache["states_at"] = 0.0
    _docker_cache["stats_at"] = 0.0

    # `compose up -d <svc>` respects depends_on - starting searxng will
    # also start redis if needed.
    return result


def _docker_stop(manifest: dict[str, Any]) -> dict[str, Any]:
    compose = _docker_compose_args(manifest)
    svc = _docker_compose_service_name(manifest)
    if not compose or not svc:
        return {"ok": False, "error": "manifest missing compose_file or compose_service"}

    state = _docker_container_state(manifest)
    was_running = bool(state and state.get("State") == "running")
    if not was_running:
        return {"ok": True, "was_running": False}

    _docker_cache["states_at"] = 0.0
    _docker_cache["stats_at"] = 0.0

    result = _docker_run(compose + ["stop", svc], timeout=30.0)
    result["was_running"] = was_running
    return result


def _docker_restart(manifest: dict[str, Any]) -> dict[str, Any]:
    """`docker compose restart` does stop+start with proper teardown wait."""
    compose = _docker_compose_args(manifest)
    svc = _docker_compose_service_name(manifest)
    if not compose or not svc:
        return {"ok": False, "error": "manifest missing compose_file or compose_service"}

    _docker_cache["states_at"] = 0.0
    _docker_cache["stats_at"] = 0.0
    return _docker_run(compose + ["restart", svc], timeout=60.0)


async def _docker_status(manifest: dict[str, Any]) -> dict[str, Any]:
    # Refresh cache (if stale) using non-blocking IO. Concurrent callers
    # share one refresh via the asyncio.Lock inside the helper.
    await _docker_refresh_cache_if_stale()

    svc = _docker_compose_service_name(manifest)
    state = _docker_container_state(manifest)

    if state is None or state.get("State") != "running":
        return {
            "service": manifest.get("service"),
            "service_type": "docker_compose",
            "running": False,
            "compose_service": svc,
        }

    container_id = state.get("ID") or ""
    stats = _docker_stats(container_id)

    # Uptime from CreatedAt - best-effort, parsing varies by docker version.
    uptime_seconds: int | None = None
    try:
        from datetime import datetime, timezone
        created_str = state.get("CreatedAt", "")
        if created_str:
            for fmt in ("%Y-%m-%d %H:%M:%S %z %Z", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    dt = datetime.strptime(created_str, fmt)
                    uptime_seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
                    break
                except ValueError:
                    continue
    except Exception:
        uptime_seconds = None

    port_health = None
    if manifests.service_has_port(manifest):
        health_url = manifest.get("health_url")
        if health_url:
            port_health = await _http_probe(health_url)
        else:
            port_health = await _pid_port_health(manifest)

    return {
        "service": manifest.get("service"),
        "service_type": "docker_compose",
        "running": True,
        "container_id": container_id,
        "compose_service": svc,
        "memory_mb": stats["memory_mb"] if stats else None,
        "cpu_percent": stats["cpu_percent"] if stats else None,
        "uptime_seconds": uptime_seconds,
        "port_health": port_health,
        "container_state": state.get("State"),
    }


def _docker_tail_log(manifest: dict[str, Any], lines: int) -> dict[str, Any]:
    """`docker compose logs --tail N <svc>` - containers don't log to a
    discoverable file path, so we shell out to compose for the tail.
    """
    compose = _docker_compose_args(manifest)
    svc = _docker_compose_service_name(manifest)
    if not compose or not svc:
        return {"ok": False, "error": "manifest missing compose_file or compose_service"}

    n = max(1, min(lines, 10_000))
    result = _docker_run(
        compose + ["logs", "--tail", str(n), "--no-color", svc],
        timeout=15.0,
    )
    if not result["ok"]:
        return {"ok": False, "error": result.get("stderr") or "docker compose logs failed"}

    # Strip the "<service>  | " prefix compose adds - without this every
    # log line wastes ~12 chars on the prefix when only one service is
    # being tailed.
    prefix = f"{svc}  | "
    cleaned = []
    for ln in result["stdout"].splitlines():
        cleaned.append(ln[len(prefix):] if ln.startswith(prefix) else ln)

    return {
        "ok": True,
        "lines": cleaned,
        "source": "docker compose logs",
    }

# ═════════════════════════════════════════════════════════════════════
# Public dispatchers - what service_routes.py calls
# ═════════════════════════════════════════════════════════════════════

def start(manifest: dict[str, Any]) -> dict[str, Any]:
    stype = manifests.service_type(manifest)
    if stype == "pid_file":      return _pid_start(manifest)
    if stype == "library":       return _library_unsupported_op("start")
    if stype == "systemd":       return _systemd_start(manifest)
    if stype == "docker_compose": return _docker_start(manifest)
    return {"ok": False, "error": f"unknown service_type: {stype}"}


def stop(manifest: dict[str, Any]) -> dict[str, Any]:
    stype = manifests.service_type(manifest)
    if stype == "pid_file":      return _pid_stop(manifest)
    if stype == "library":       return _library_unsupported_op("stop")
    if stype == "systemd":       return _systemd_stop(manifest)
    if stype == "docker_compose": return _docker_stop(manifest)
    return {"ok": False, "error": f"unknown service_type: {stype}"}


def restart(manifest: dict[str, Any]) -> dict[str, Any]:
    """pid_file uses the port-release dance to fix the historical
    'stop fires but new process can't bind' bug. systemd and
    docker_compose use their native restart commands which handle
    teardown internally."""
    stype = manifests.service_type(manifest)

    if stype == "pid_file":
        stop_res = _pid_stop(manifest)
        port = int(manifest.get("port") or 0)
        port_released = _wait_for_port_release(port, timeout_s=10.0)
        if not port_released:
            return {
                "ok": False,
                "stop": stop_res,
                "start": {
                    "ok": False,
                    "error": f"port {port} still held 10s after stop - refusing to start",
                    "hint": f"check: sudo lsof -i :{port}",
                },
            }
        start_res = _pid_start(manifest)
        return {
            "ok": start_res.get("ok", False),
            "stop": stop_res,
            "start": start_res,
            "port_released": port_released,
        }

    if stype == "library":        return _library_unsupported_op("restart")
    if stype == "systemd":        return _systemd_restart(manifest)
    if stype == "docker_compose": return _docker_restart(manifest)
    return {"ok": False, "error": f"unknown service_type: {stype}"}


async def status(manifest: dict[str, Any]) -> dict[str, Any]:
    """Comprehensive status. Every handler returns the same shape so the
    API caller doesn't care about service_type."""
    stype = manifests.service_type(manifest)
    if stype == "pid_file":       return await _pid_status(manifest)
    if stype == "library":        return await _library_status(manifest)
    if stype == "systemd":        return await _systemd_status(manifest)
    if stype == "docker_compose": return await _docker_status(manifest)
    return {
        "service": manifest.get("service"),
        "service_type": stype,
        "running": False,
        "error": f"unknown service_type: {stype}",
    }


def tail_log(manifest: dict[str, Any], lines: int = 100) -> dict[str, Any]:
    """Tail the last N lines of the service's logs.

    Most service types log to a file (~/seren-logs/<name>.log). Docker is
    the exception - logs live in the daemon's storage, so we shell out to
    `docker compose logs`.

    For file-based services, falls back to ~/seren-logs/<name>.log if the
    manifest doesn't declare log_path explicitly (same convention pattern
    as PID files + start/stop scripts).
    """
    stype = manifests.service_type(manifest)
    if stype == "docker_compose":
        return _docker_tail_log(manifest, lines)

    log_path = manifest.get("log_path")
    if not log_path:
        name = manifest.get("service")
        if name:
            log_path = f"{Path.home()}/seren-logs/{name}.log"

    return _tail_file(log_path, lines)


async def probe_port(manifest: dict[str, Any]) -> dict[str, Any]:
    """Probe the service's port for a quick health check.

    Used by service_routes.py's /health endpoint - a lighter-weight check
    than full status() which also reads memory/cpu. Service-type aware:
    falls back to the manifest's health_url for systemd/docker services
    if specified, otherwise hits /health then / on the declared port.

    Library-mode services should be filtered out by the caller (no port).
    """
    if not manifests.service_has_port(manifest):
        return {"ok": False, "reason": "library-mode (no port)"}

    health_url = manifest.get("health_url")
    if health_url:
        return await _http_probe(health_url)
    return await _pid_port_health(manifest)


# ═════════════════════════════════════════════════════════════════════
# Backwards-compat shims - pre-Path-C callers used these names directly.
# ═════════════════════════════════════════════════════════════════════


def read_pid(manifest: dict[str, Any]) -> int | None:
    """Public shim: return the running PID for any service type, or None.

    system_routes.py uses this to check whether a service is running before
    deciding whether to call stop() during reclaim. Works across all service
    types:
      - pid_file   → reads the PID file
      - systemd    → queries MainPID from systemctl show
      - docker_compose → checks container State == running
      - library    → always None (no daemon)
    """
    stype = manifests.service_type(manifest)
    if stype == "pid_file":
        return _pid_read(manifest)
    if stype == "systemd":
        unit = _systemd_unit_name(manifest)
        if not unit or not _systemd_is_active(unit):
            return None
        props = _systemd_show(unit, ["MainPID"])
        try:
            pid = int(props.get("MainPID", "0"))
            return pid if pid > 0 else None
        except ValueError:
            return None
    if stype == "docker_compose":
        state = _docker_container_state(manifest)
        if state and state.get("State") == "running":
            return 1  # sentinel: "is running"; no single PID for compose
        return None
    return None  # library or unknown
read_pid = _pid_read  # historical name; service_routes uses this name