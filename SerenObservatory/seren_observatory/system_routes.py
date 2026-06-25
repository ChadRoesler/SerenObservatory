"""
/api/v1/system/* - node-level endpoints.

These describe the box itself, aggregate service state, and expose
orchestration primitives (reclaim).
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile

from . import __version__, lifecycle, manifests

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/ping")
async def ping() -> dict[str, Any]:
    """Cheap liveness probe. PUBLIC - bypasses auth (see auth.PUBLIC_PATHS)."""
    return {"ok": True, "ts": int(time.time())}


@router.get("/version")
async def version() -> dict[str, Any]:
    """Observatory version + manifest schema. PUBLIC - bypasses auth."""
    return {
        "observatory_version": __version__,
        "manifest_schema": manifests.SCHEMA_VERSION,
    }


@router.get("/node")
async def node_info() -> dict[str, Any]:
    """Returns ~/.seren/node.json plus runtime stats (load, free memory, etc).

    For the dashboard's "what is this box?" panel.
    """
    n = manifests.load_node()

    # Runtime stats - cheap to compute
    runtime: dict[str, Any] = {}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            runtime["load_avg"] = [float(p) for p in parts[:3]]
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            mem: dict[str, int] = {}
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                # value looks like "  123456 kB"
                val = v.strip().split()
                if val and val[0].isdigit():
                    mem[k] = int(val[0])
            total_mb = mem.get("MemTotal", 0) // 1024
            avail_mb = mem.get("MemAvailable", 0) // 1024
            runtime["memory_mb_total"] = total_mb
            runtime["memory_mb_available"] = avail_mb
            if total_mb:
                runtime["memory_pct_used"] = round(
                    100 * (total_mb - avail_mb) / total_mb, 1
                )
    except OSError:
        pass
    try:
        with open("/proc/uptime") as f:
            runtime["uptime_seconds"] = int(float(f.read().split()[0]))
    except OSError:
        pass

    return {"manifest": n, "runtime": runtime}


@router.get("/thermal")
async def thermal() -> dict[str, Any]:
    """Per-zone temperature readings from the kernel's thermal subsystem.

    Two filesystem layouts in the wild, both checked:
      - /sys/class/thermal/thermal_zone*       - most common
      - /sys/devices/virtual/thermal/thermal_zone*  - Orin Nano + some
        custom L4T BSPs expose zones HERE instead, with /sys/class/thermal
        either empty or missing entirely.

    Jetsons typically expose 5-8 zones (CPU, GPU, AUX, AO, etc.); NUCs
    typically expose 1-2 (CPU package). Each zone's `type` is a vendor
    string like 'CPU-therm' or 'x86_pkg_temp' or 'cpu-thermal'.

    Some zones return EAGAIN ("Resource temporarily unavailable") on
    Orin under specific conditions (cv0/cv1-thermal etc) - we silently
    skip those zones rather than failing the whole call.

    Dashboard typically takes max(temps) for a single "node hotness"
    value. Returns available=False on hosts with no thermal interface
    (VMs, weird hardware).
    """
    import glob
    import os

    # Both candidate roots. We dedupe by zone type+temp at the end so
    # that if a host happens to expose zones in BOTH places (some BSPs
    # do), we don't double-count.
    candidate_roots = [
        "/sys/class/thermal",
        "/sys/devices/virtual/thermal",
    ]

    seen_zones: dict[str, dict[str, Any]] = {}  # keyed by type to dedupe

    for root in candidate_roots:
        if not os.path.isdir(root):
            continue
        try:
            for zone_dir in sorted(glob.glob(f"{root}/thermal_zone*")):
                try:
                    with open(os.path.join(zone_dir, "type")) as f:
                        ztype = f.read().strip()
                    with open(os.path.join(zone_dir, "temp")) as f:
                        # millidegrees C → degrees C
                        temp_c = int(f.read().strip()) / 1000.0
                except (OSError, ValueError, TypeError):
                    # Zone unreadable. Several failure modes seen in the
                    # wild, all silently skipped:
                    #   - OSError / BlockingIOError: clean EAGAIN
                    #   - ValueError: temp file contains non-numeric junk
                    #   - TypeError: Python's text I/O layer can raise
                    #     "can't concat NoneType to bytes" when sysfs read()
                    #     returns None mid-decode. Observed on Orin Nano's
                    #     cv0/cv1/cv2-thermal zones - these are CV-engine
                    #     temperature sensors that aren't always active and
                    #     EAGAIN through Python comes back as TypeError, not
                    #     the cleaner BlockingIOError. The kernel is
                    #     consistent (EAGAIN); Python's wrapping is not.
                    continue

                # Sentinel filtering - drop obviously-broken values
                # (negative, absurdly high). We DON'T filter "100°C
                # sentinel" because some BSPs report a real 100°C and
                # we don't want to hide a thermal emergency by accident.
                # If a Jetson zone always reports 100°C while clearly
                # idle, that's a hardware/BSP quirk - better to surface
                # it noisily than silently lie.
                if temp_c < -40.0 or temp_c > 200.0:
                    continue

                # Dedup by type - first appearance wins (we list the
                # /sys/class root first, which is canonical when both
                # paths exist).
                if ztype not in seen_zones:
                    seen_zones[ztype] = {
                        "zone": os.path.basename(zone_dir),
                        "type": ztype,
                        "temp_c": round(temp_c, 1),
                    }
        except OSError:
            # Whole root unreadable - try the next one.
            continue

    zones = list(seen_zones.values())

    if not zones:
        return {"available": False, "zones": [], "max_temp_c": None}

    return {
        "available": True,
        "zones": zones,
        "max_temp_c": max(z["temp_c"] for z in zones),
    }


@router.get("/services")
async def services_summary() -> dict[str, Any]:
    """Lists every installed service and its current runtime status.

    The dashboard hits this once for a full picture instead of N round-trips.
    """
    all_manifests = manifests.load_services()

    # Probe lifecycle status for everything in parallel - cheaper than serial
    async def probe(name: str, m: dict) -> tuple[str, dict]:
        return name, await lifecycle.status(m)

    if all_manifests:
        results = await asyncio.gather(*[probe(n, m) for n, m in all_manifests.items()])
        statuses = dict(results)
    else:
        statuses = {}

    return {
        "count": len(all_manifests),
        "services": {
            name: {"manifest": m, "status": statuses.get(name)}
            for name, m in all_manifests.items()
        },
    }


@router.get("/health")
async def system_health() -> dict[str, Any]:
    """Rollup health: 200 if every running service is healthy, 503 otherwise.

    Suitable for dashboard health lights and SCC's cluster-wide rollup.
    """
    all_manifests = manifests.load_services()

    healthy_count = 0
    degraded: list[str] = []
    not_running: list[str] = []

    async def assess(name: str, m: dict) -> None:
        nonlocal healthy_count
        st = await lifecycle.status(m)
        if not st.get("running"):
            # Library-mode services don't "run" so they're not degraded
            if st.get("library_mode"):
                healthy_count += 1
                return
            not_running.append(name)
            return
        port_health = st.get("port_health")
        if port_health is None or port_health.get("ok"):
            healthy_count += 1
        else:
            degraded.append(name)

    if all_manifests:
        await asyncio.gather(*[assess(n, m) for n, m in all_manifests.items()])

    overall = len(degraded) == 0 and len(not_running) == 0
    return {
        "ok": overall,
        "total": len(all_manifests),
        "healthy": healthy_count,
        "degraded": degraded,
        "not_running": not_running,
    }


@router.post("/reclaim")
async def reclaim(body: dict | None = None) -> dict[str, Any]:
    """Stop idle services to free GPU memory.

    Body (optional):
        target_free_mb: int     - release until at least this much GPU/system memory free
        exclude:        [str]   - service names to never stop
        policy:         str     - "idle" (default) | "aggressive"

    Currently the implementation is simple: stops every service except
    those in `exclude`, regardless of "idle" semantics. A future version
    would track per-service last-request timestamps and only stop ones
    above an idle threshold. For now this gives the dashboard a working
    "free up memory" button.
    """
    body = body or {}
    exclude = set(body.get("exclude", []))
    all_manifests = manifests.load_services()

    stopped: list[str] = []
    kept: list[str] = []
    failed: list[dict] = []

    for name, m in all_manifests.items():
        if name in exclude:
            kept.append(name)
            continue
        if not manifests.service_has_lifecycle(m):
            kept.append(name)  # library-mode services aren't stoppable
            continue
        if lifecycle.read_pid(m) is None:
            continue  # already stopped
        result = lifecycle.stop(m)
        if result.get("ok"):
            stopped.append(name)
        else:
            failed.append({"service": name, "error": result.get("error")})

    return {
        "stopped": stopped,
        "kept": kept,
        "failed": failed,
    }

@router.post("/reboot")
async def reboot(body: dict | None = None) -> dict[str, Any]:
    """Schedule a system reboot via `sudo shutdown -r +1`.

    Body (optional):
        delay_minutes: int - minutes from now to reboot. Default 1.
                             Min 0 (= "now"), max 60.

    Returns:
        scheduled: bool   - true if the reboot was queued
        scheduled_at: str - ISO timestamp when reboot will fire (best-effort)
        delay_minutes: int - what we asked for
        method: str       - "shutdown -r +N" (informational)

    The 1-minute default is intentional: it gives the HTTP response time
    to flush back to the caller AND a window where you can `sudo shutdown
    -c` (or POST /reboot/cancel) if you fat-fingered the dashboard.

    Requires sudoers grant for /sbin/shutdown -r * - added by host-setup.sh
    and seren-setup.sh's install_observatory_common. Existing installs need the
    seren-sudoers-update.sh migration to add the new grant.
    """
    import datetime
    import shlex
    import subprocess

    body = body or {}
    delay = int(body.get("delay_minutes", 1))
    delay = max(0, min(60, delay))  # clamp 0..60

    # `shutdown -r +0` and `shutdown -r now` both mean "immediate" but the
    # +N form is what we use for non-zero delays - keep one code path.
    when_arg = "now" if delay == 0 else f"+{delay}"
    cmd = ["sudo", "-n", "/sbin/shutdown", "-r", when_arg]

    # Run non-interactively (`-n`) - if sudoers isn't configured, fail
    # fast rather than hang on a password prompt.
    try:
        # capture stderr for the error path; shutdown -r +N exits 0
        # immediately after queuing the broadcast, doesn't block.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {
            "scheduled": False,
            "error": "shutdown command timed out (5s) - sudoers may not be configured",
            "command": shlex.join(cmd),
        }

    if result.returncode != 0:
        return {
            "scheduled": False,
            "error": result.stderr.strip() or f"shutdown exited {result.returncode}",
            "command": shlex.join(cmd),
            "hint": (
                "If stderr mentions 'a password is required', the observatory's "
                "sudoers file is missing the /sbin/shutdown grant. Run "
                "seren-sudoers-update.sh on this node (or re-run host-setup.sh)."
            ),
        }

    # Best-effort scheduled timestamp. We don't ask shutdown for this -
    # it's just "now + delay minutes" computed locally.
    scheduled_at = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=delay)
    ).isoformat(timespec="seconds")

    return {
        "scheduled": True,
        "scheduled_at": scheduled_at,
        "delay_minutes": delay,
        "method": f"shutdown -r {when_arg}",
    }


@router.post("/reboot/cancel")
async def reboot_cancel() -> dict[str, Any]:
    """Cancel a previously scheduled `shutdown -r`.

    Use case: the dashboard's reboot confirmation has a "Cancel" button
    that fires during the 1-minute warning window. This endpoint maps
    directly to `sudo shutdown -c`.

    Returns:
        cancelled: bool   - true if shutdown -c exited cleanly
        error: str        - populated when cancelled=false

    Requires sudoers grant for /sbin/shutdown -c (added alongside the -r
    grant in install_observatory_common).
    """
    import shlex
    import subprocess

    cmd = ["sudo", "-n", "/sbin/shutdown", "-c"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {
            "cancelled": False,
            "error": "shutdown -c timed out - sudoers may not be configured",
            "command": shlex.join(cmd),
        }

    # `shutdown -c` exits 0 even if there was nothing scheduled. We
    # surface that to the caller so they can decide whether it's an
    # expected no-op or unexpected.
    if result.returncode != 0:
        return {
            "cancelled": False,
            "error": result.stderr.strip() or f"shutdown -c exited {result.returncode}",
            "command": shlex.join(cmd),
        }

    return {"cancelled": True}


@router.post("/observatory-update")
async def observatory_update(
    package: UploadFile = File(...),
    dest_path: str = Form(...),
) -> dict[str, Any]:
    """Receive a seren-observatory.tar.gz from the RuntimeHost and run the update script.

    The RuntimeHost streams the package as multipart/form-data with two parts:
        package   - the tar.gz file bytes
        dest_path - absolute path on this node where the file should land
                    (e.g. /home/seren/seren-install). The seren-observatory-update.sh
                    script must live in the same directory.

    The file is saved synchronously, then seren-observatory-update.sh is launched as
    a detached background process before we return. The update script is expected
    to restart the observatory - the response is sent first so the HTTP connection
    closes cleanly before the process is replaced.

    Returns:
        ok:      bool  - true if the file was saved and the script was launched
        message: str   - human-readable status
        error:   str   - populated on failure
    """
    import shlex
    import subprocess

    dest = dest_path.strip()
    if not dest:
        return {"ok": False, "message": None, "error": "dest_path is empty"}

    dest_dir = os.path.expanduser(dest)
    tar_path = os.path.join(dest_dir, "seren-observatory.tar.gz")
    script_path = os.path.join(dest_dir, "seren-observatory-update.sh")

    # Ensure destination directory exists
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "message": None, "error": f"could not create dest dir: {exc}"}

    # Save the uploaded package
    try:
        contents = await package.read()
        with open(tar_path, "wb") as f:
            f.write(contents)
    except OSError as exc:
        return {"ok": False, "message": None, "error": f"could not write package: {exc}"}

    # Validate the update script is present before we hand off
    if not os.path.isfile(script_path):
        return {
            "ok": False,
            "message": None,
            "error": f"update script not found at: {script_path}",
        }

    # Launch the update script detached from the observatory's process group.
    #
    # seren-observatory.service uses KillMode=process (see common.sh), which means
    # systemd only sends SIGTERM to the main Python process when the service
    # is stopped - it does NOT kill the entire cgroup. Combined with
    # start_new_session=True here (which calls setsid), the bash child is in
    # its own session and survives the observatory being stopped, then brings it
    # back up via sudo systemctl start.
    try:
        subprocess.Popen(
            ["bash", script_path],
            cwd=dest_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return {"ok": False, "message": None, "error": f"could not launch update script: {exc}"}

    return {
        "ok": True,
        "message": f"package saved to {tar_path}, update script launched",
        "error": None,
    }
