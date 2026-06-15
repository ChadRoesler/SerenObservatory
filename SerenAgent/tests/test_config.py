"""Tests for seren_agent.config.load_config().

Critically asserts that bearer_token in the yaml is IGNORED (the token is a
secrets.json safety interlock, not a config field).
"""
from __future__ import annotations

import pytest

from seren_agent.config import AgentConfig, load_config


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "seren-agent.yaml"
    monkeypatch.setenv("SEREN_AGENT_CONFIG", str(p))
    for k in ("AGENT_HOST", "AGENT_PORT", "SEREN_AGENT_HOST", "SEREN_AGENT_PORT"):
        monkeypatch.delenv(k, raising=False)
    return p


def test_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("SEREN_AGENT_CONFIG", str(tmp_path / "nope.yaml"))
    for k in ("AGENT_HOST", "AGENT_PORT", "SEREN_AGENT_HOST", "SEREN_AGENT_PORT"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 7777


def test_yaml_server_block(cfg_path):
    cfg_path.write_text("server:\n  host: 127.0.0.1\n  port: 9999\n")
    cfg = load_config()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 9999


def test_config_arg_resolves(tmp_path, monkeypatch):
    for k in ("AGENT_HOST", "AGENT_PORT", "SEREN_AGENT_HOST",
              "SEREN_AGENT_PORT", "SEREN_AGENT_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("server:\n  port: 4242\n")
    cfg = load_config(str(explicit))
    assert cfg.port == 4242


def test_bearer_token_in_yaml_is_ignored(cfg_path, capsys):
    """THE load-bearing test: a token in the yaml must NOT become the agent's
    auth token. It's ignored with a loud note; auth stays on secrets.json."""
    cfg_path.write_text(
        "server:\n"
        "  host: 0.0.0.0\n"
        "  port: 7777\n"
        "  bearer_token: sneaky-token-that-should-be-ignored\n"
    )
    cfg = load_config()
    # cfg has no token attribute at all
    assert not hasattr(cfg, "bearer_token")
    assert not hasattr(cfg, "token")
    # and the value never leaks into host/port
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 7777
    captured = capsys.readouterr()
    assert "ignored by design" in (captured.out + captured.err)


def test_env_overrides_yaml(cfg_path, monkeypatch):
    cfg_path.write_text("server:\n  port: 9999\n")
    monkeypatch.setenv("AGENT_PORT", "5555")
    cfg = load_config()
    assert cfg.port == 5555


def test_seren_agent_env_alias(cfg_path, monkeypatch):
    cfg_path.write_text("server:\n  port: 9999\n")
    monkeypatch.setenv("SEREN_AGENT_PORT", "6666")
    cfg = load_config()
    assert cfg.port == 6666


def test_malformed_yaml_falls_back(cfg_path, capsys):
    cfg_path.write_text("server:\n  port: [bad: yaml\n")
    cfg = load_config()
    assert cfg.port == 7777
    captured = capsys.readouterr()
    assert "failed to parse" in (captured.out + captured.err)


def test_bad_port_value_falls_back(cfg_path, capsys):
    cfg_path.write_text("server:\n  port: not-a-number\n  host: 10.0.0.5\n")
    cfg = load_config()
    assert cfg.port == 7777          # bad value falls back
    assert cfg.host == "10.0.0.5"    # good value applies
    captured = capsys.readouterr()
    assert "ignored bad value for 'port'" in (captured.out + captured.err)


def test_unknown_key_ignored(cfg_path, capsys):
    cfg_path.write_text("server:\n  port: 8888\n  enable_skynet: true\n")
    cfg = load_config()
    assert cfg.port == 8888
    captured = capsys.readouterr()
    assert "unknown server key 'enable_skynet'" in (captured.out + captured.err)


def test_default_host_is_cluster_bind():
    """Agent follows the leader on structure but keeps its 0.0.0.0 cluster
    bind (it's a LAN plane; the auth interlock is the guard, not the bind)."""
    assert AgentConfig().host == "0.0.0.0"
