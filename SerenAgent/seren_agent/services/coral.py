"""
Coral M.2 TPU service-specific endpoints.

Mounted under /api/v1/service/coral.

Coral is hardware (not a daemon) and library-mode (no HTTP port). The
endpoints expose what state we can observe: device presence, kernel
module load, and the ability to run the smoke test.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import manifests


def register(router: APIRouter) -> None:
    @router.get("/device")
    async def device_status():
        """Reports whether the Coral PCIe device is present + accessible."""
        m = manifests.load_service("coral")
        if m is None:
            raise HTTPException(404, "coral not installed on this node")

        device_path = m.get("serviceSpecific", {}).get("device_path", "/dev/apex_0")
        present = Path(device_path).exists()

        # Check whether the kernel modules are loaded
        modules_loaded: dict[str, bool] = {}
        try:
            with open("/proc/modules") as f:
                loaded = {line.split()[0] for line in f}
            modules_loaded = {
                "gasket": "gasket" in loaded,
                "apex":   "apex" in loaded,
            }
        except OSError:
            pass

        # Permissions on the device - the user needs read access
        readable = False
        if present:
            try:
                readable = os.access(device_path, os.R_OK | os.W_OK)
            except OSError:
                pass

        return {
            "device_path": device_path,
            "present": present,
            "readable": readable,
            "modules": modules_loaded,
        }

    @router.post("/test")
    async def run_test():
        """Invoke ~/test-coral.sh to verify pycoral can talk to the device.

        Returns the test's stdout/stderr - useful for the dashboard to show
        a green/red light without the user SSHing in.
        """
        m = manifests.load_service("coral")
        if m is None:
            raise HTTPException(404, "coral not installed on this node")

        test_script = m.get("serviceSpecific", {}).get("test_script")
        if not test_script or not Path(test_script).is_file():
            raise HTTPException(404, f"test script not found: {test_script}")

        try:
            result = subprocess.run(
                ["bash", test_script],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "test script timed out after 60s")

        return {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
