"""
Tests for seren_observatory.manifests - manifest loading and service_type resolution.
"""
from __future__ import annotations

import json

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
