"""
Shared pytest fixtures for seren-observatory tests.

Uses tmp_path to build a fake ~/.seren manifest tree so tests never touch
the real filesystem.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def offline_update_checks(monkeypatch):
    """No test may talk to pypi.org.

    The info route carries the update status and update checking is ON by
    default, so without this every test that touches it would make a real
    network call - slow, flaky offline, and rude to someone else's server.

    Patching the CLASS method rather than an env var is deliberate: some tests
    build a config object directly instead of going through load_config, so an
    env override wouldn't reach them. The checker still runs and still returns
    a well-formed status - just status="error" instead of a real answer, which
    is exactly what a box with no internet would see.
    """
    try:
        from seren_meninges.updates import UpdateChecker
    except ImportError:
        return  # meninges without the updates module, nothing to muzzle

    async def _no_network(self, distribution):
        raise ConnectionError("network disabled in tests")

    # Must be patched BEFORE any UpdateChecker is constructed - __init__ binds
    # self._fetch = fetcher or self._fetch_from_index. autouse + function scope
    # puts it in place before the app lifespan runs.
    monkeypatch.setattr(UpdateChecker, "_fetch_from_index", _no_network)


# ── fake home + manifest layout ──────────────────────────────────────────

@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME and the manifests module to a temp directory tree."""
    seren_dir = tmp_path / ".seren"
    seren_dir.mkdir()
    (seren_dir / "services").mkdir()

    # Patch os.path.expanduser so Path("~") resolves to tmp_path
    monkeypatch.setenv("HOME", str(tmp_path))

    # Patch manifests module-level paths directly
    import seren_observatory.manifests as m
    monkeypatch.setattr(m, "HOME", tmp_path)
    monkeypatch.setattr(m, "MANIFEST_DIR", seren_dir)
    monkeypatch.setattr(m, "SERVICES_DIR", seren_dir / "services")

    return tmp_path


@pytest.fixture()
def node_manifest(fake_home: Path) -> dict:
    """Write and return a minimal node.json."""
    data = {
        "schema_version": 2,
        "hostname": "test-jetson",
        "hardware": "Jetson AGX Orin",
    }
    path = fake_home / ".seren" / "node.json"
    path.write_text(json.dumps(data))
    return data


@pytest.fixture()
def pid_service_manifest(fake_home: Path) -> dict:
    """Write a minimal pid_file service manifest and return it."""
    data = {
        "schema_version": 2,
        "service": "llama",
        "service_type": "pid_file",
        "port": 8080,
    }
    path = fake_home / ".seren" / "services" / "llama.json"
    path.write_text(json.dumps(data))
    return data


@pytest.fixture()
def library_service_manifest(fake_home: Path) -> dict:
    """Write a minimal library service manifest and return it."""
    data = {
        "schema_version": 2,
        "service": "coral",
        "service_type": "library",
        "port": 0,
    }
    path = fake_home / ".seren" / "services" / "coral.json"
    path.write_text(json.dumps(data))
    return data
