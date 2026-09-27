"""
Tests for seren_observatory.manifests - manifest loading and service_type resolution.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from seren_observatory import manifests


class TestLoadNode:
    def test_returns_none_when_missing(self, fake_home):
        assert manifests.load_node() is None

    def test_loads_valid_node(self, node_manifest, fake_home):
        node = manifests.load_node()
        assert node is not None
        assert node["hostname"] == "test-jetson"

    def test_rejects_future_schema(self, fake_home):
        path = fake_home / ".seren" / "node.json"
        path.write_text(json.dumps({"schema_version": 999}))
        assert manifests.load_node() is None

    def test_handles_bad_json(self, fake_home):
        path = fake_home / ".seren" / "node.json"
        path.write_text("not json {{{")
        assert manifests.load_node() is None


class TestLoadServices:
    def test_empty_when_no_services(self, fake_home):
        result = manifests.load_services()
        assert result == {}

    def test_loads_pid_service(self, pid_service_manifest, fake_home):
        result = manifests.load_services()
        assert "llama" in result
        assert result["llama"]["service_type"] == "pid_file"

    def test_skips_invalid_json(self, fake_home):
        bad = fake_home / ".seren" / "services" / "bad.json"
        bad.write_text("{{{{")
        result = manifests.load_services()
        assert "bad" not in result

    def test_skips_future_schema(self, fake_home):
        path = fake_home / ".seren" / "services" / "future.json"
        path.write_text(json.dumps({"schema_version": 999, "service": "future"}))
        result = manifests.load_services()
        assert "future" not in result


class TestServiceType:
    def test_explicit_pid_file(self):
        m = {"service_type": "pid_file", "port": 8080}
        assert manifests.service_type(m) == "pid_file"

    def test_explicit_library(self):
        m = {"service_type": "library", "port": 0}
        assert manifests.service_type(m) == "library"

    def test_explicit_systemd(self):
        m = {"service_type": "systemd", "systemd_unit": "myunit.service"}
        assert manifests.service_type(m) == "systemd"

    def test_explicit_docker_compose(self):
        m = {"service_type": "docker_compose", "compose_file": "/path/docker-compose.yml"}
        assert manifests.service_type(m) == "docker_compose"

    def test_infers_library_from_zero_port(self):
        m = {"port": 0}
        assert manifests.service_type(m) == "library"

    def test_infers_pid_file_from_positive_port(self):
        m = {"port": 8080}
        assert manifests.service_type(m) == "pid_file"

    def test_servicespecific_managed_by_systemd(self):
        m = {"port": 7777, "serviceSpecific": {"managed_by": "systemd"}}
        assert manifests.service_type(m) == "systemd"

    def test_unknown_type_falls_back_by_port(self):
        # Unknown explicit service_type: port-based fallback
        m = {"service_type": "unknown_future_type", "port": 9999}
        # Not in SERVICE_TYPES → falls through to port inference
        assert manifests.service_type(m) == "pid_file"


class TestServiceHelpers:
    def test_service_has_port_true(self):
        assert manifests.service_has_port({"port": 8080}) is True

    def test_service_has_port_false_zero(self):
        assert manifests.service_has_port({"port": 0}) is False

    def test_service_has_port_false_missing(self):
        assert manifests.service_has_port({}) is False

    def test_service_has_lifecycle_pid_file(self):
        assert manifests.service_has_lifecycle({"service_type": "pid_file"}) is True

    def test_service_has_lifecycle_library(self):
        assert manifests.service_has_lifecycle({"service_type": "library"}) is False


class TestManifestsDir:
    """Where the roster is: $SEREN_OBSERVATORY_MANIFESTS -> the yaml's
    server.manifests_dir -> ~/.seren/services. Resolved at call time so two
    installs on one host (~/seren/<install>/manifests) keep separate rosters."""

    def test_default_is_home_dot_seren_services(self, fake_home):
        assert manifests.resolve_manifests_dir() == fake_home / ".seren" / "services"
        assert manifests.resolve_manifests_dir() == Path(os.path.expanduser("~/.seren/services"))

    def test_configured_dir_is_used_and_expanded(self, fake_home):
        got = manifests.resolve_manifests_dir("~/seren/alpha/manifests")
        assert got == fake_home / "seren" / "alpha" / "manifests"

    def test_recorded_config_is_used(self, fake_home):
        """create_app hands the yaml's value over via configure(); a bare
        resolve (what every route does) must honour it."""
        manifests.configure("~/seren/alpha/manifests")
        assert manifests.resolve_manifests_dir() == fake_home / "seren" / "alpha" / "manifests"

    def test_env_beats_configured(self, fake_home, monkeypatch):
        manifests.configure("~/seren/alpha/manifests")
        monkeypatch.setenv(manifests.MANIFESTS_ENV, "~/seren/beta/manifests")
        assert manifests.resolve_manifests_dir() == fake_home / "seren" / "beta" / "manifests"
        got = manifests.resolve_manifests_dir("~/seren/alpha/manifests")
        assert got == fake_home / "seren" / "beta" / "manifests"

    def test_empty_env_counts_as_unset(self, fake_home, monkeypatch):
        monkeypatch.setenv(manifests.MANIFESTS_ENV, "")
        got = manifests.resolve_manifests_dir("~/seren/alpha/manifests")
        assert got == fake_home / "seren" / "alpha" / "manifests"

    def test_not_frozen_at_import(self, fake_home, monkeypatch):
        """A bare load_services() honours an env var set AFTER import."""
        roster = fake_home / "seren" / "gamma" / "manifests"
        roster.mkdir(parents=True)
        (roster / "kokoro.json").write_text(json.dumps({"schema_version": 2, "service": "kokoro"}))
        assert "kokoro" not in manifests.load_services()
        monkeypatch.setenv(manifests.MANIFESTS_ENV, str(roster))
        assert list(manifests.load_services()) == ["kokoro"]

    def test_default_config_is_one_box_roster(self, pid_service_manifest, fake_home):
        """No knob set: one directory, ~/.seren/services, exactly as before -
        the only difference is the _roster marker."""
        assert manifests.rosters() == [("box", fake_home / ".seren" / "services")]
        assert manifests.load_services() == {"llama": {**pid_service_manifest, "_roster": "box"}}

    def test_node_json_stays_per_box(self, node_manifest, fake_home):
        """A node is one box: moving the roster must not move node.json."""
        manifests.configure("~/seren/alpha/manifests")
        assert manifests.node_path() == fake_home / ".seren" / "node.json"
        assert manifests.load_node()["hostname"] == "test-jetson"


class TestTwoRosters:
    """A moved install roster is read TOGETHER with the box roster
    (~/.seren/services), where the node installers put the GPU daemons.
    Install wins a name clash; one directory is never read twice."""

    @staticmethod
    def _write(d: Path, name: str, **extra) -> dict:
        d.mkdir(parents=True, exist_ok=True)
        data = {"schema_version": 2, "service": name, "service_type": "pid_file",
                "port": 8090, **extra}
        (d / f"{name}.json").write_text(json.dumps(data))
        return data

    def test_both_rosters_merged(self, pid_service_manifest, fake_home):
        alpha = fake_home / "seren" / "alpha" / "manifests"
        self._write(alpha, "seren-memory-alpha", service_type="systemd")
        manifests.configure(alpha)
        assert manifests.rosters() == [("install", alpha),
                                       ("box", fake_home / ".seren" / "services")]
        got = manifests.load_services()
        assert sorted(got) == ["llama", "seren-memory-alpha"]
        assert got["seren-memory-alpha"]["_roster"] == "install"
        assert got["llama"]["_roster"] == "box"

    def test_install_wins_a_clash(self, pid_service_manifest, fake_home):
        alpha = fake_home / "seren" / "alpha" / "manifests"
        self._write(alpha, "llama", port=9999)
        manifests.configure(alpha)
        got = manifests.load_services()
        assert got["llama"]["port"] == 9999
        assert got["llama"]["_roster"] == "install"

    def test_same_dir_read_once(self, pid_service_manifest, fake_home, monkeypatch):
        """The configured roster IS the box roster, spelled differently."""
        monkeypatch.setenv(manifests.MANIFESTS_ENV,
                           str(fake_home / ".seren" / "services" / ".." / "services") + os.sep)
        assert manifests.rosters() == [("box", fake_home / ".seren" / "services")]
        reads = []
        real_open = open

        def counting_open(path, *a, **k):
            reads.append(Path(path).name)
            return real_open(path, *a, **k)
        monkeypatch.setattr("builtins.open", counting_open)
        got = manifests.load_services()
        assert list(got) == ["llama"] and got["llama"]["_roster"] == "box"
        assert reads.count("llama.json") == 1

    def test_missing_install_roster_still_lists_the_box(self, pid_service_manifest, fake_home):
        manifests.configure(fake_home / "seren" / "never-made" / "manifests")
        assert list(manifests.load_services()) == ["llama"]
