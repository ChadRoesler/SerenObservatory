"""
Shared pytest fixtures for seren-agent tests.

Uses tmp_path to build a fake ~/.seren manifest tree so tests never touch
the real filesystem.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


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
    import seren_agent.manifests as m
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
