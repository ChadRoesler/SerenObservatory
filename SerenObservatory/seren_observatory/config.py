"""Config for seren-observatory.

Follows the SerenMemory convention (Memory leads, the rest follow) so a buddy
who set up one service already knows how to set up this one:

    * network settings live under a ``server:`` block (host/port)
    * config resolves: --config  ->  $SEREN_AGENT_CONFIG  ->
      ~/seren-observatory/seren-observatory.yaml  ->  built-in defaults
    * the file is named seren-observatory.yaml

DELIBERATE EXCEPTION - the bearer token is NOT here. Unlike SerenMemory, the
observatory's token is a SAFETY INTERLOCK, not a config knob: this plane can restart
services and trigger a sudoers-backed reboot, so the token lives in
~/.seren/secrets.json (chmod 600, written by seren-secrets.sh) and is loaded
by auth.load_token(). The observatory fails CLOSED on mutating methods when no token
exists. Putting the token in a yaml field would add a second, lower-security
path (yaml may be 644, may be committed) next to the deliberate secrets.json
one - a security regression dressed as consistency. Follow-the-leader on
STRUCTURE (--config, server: block, resolution order); NOT on collapsing auth
into config. (Same spirit as Margin keeping 127.0.0.1 instead of inheriting
Memory's 0.0.0.0.)

Precedence (highest wins):
    1. Env vars  (AGENT_HOST/AGENT_PORT, and the SEREN_AGENT_* aliases)
    2. YAML file (operator's standing config)
    3. Defaults  (0.0.0.0:7777 - the Seren cluster convention)

Lenient parse (Postel-as-kindness): missing file or malformed YAML -> log and
fall back to defaults; a bad single value -> that key falls back, others apply.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

try:
    import yaml  # type: ignore[import-untyped]
    _HAS_YAML = True
except ImportError:  # pragma: no cover - pyyaml is a hard dep, but be lenient
    _HAS_YAML = False


class ObservatoryConfig(BaseModel):
    """seren-observatory network config. Defaults match the cluster convention:
    bind all interfaces (trusted LAN) on 7777.

    NOTE: no token field here, on purpose - see module docstring.
    """

    # Bind all interfaces by default - the observatory is a cluster plane meant to be
    # reached from the NUC/RuntimeHost across the trusted LAN. (Contrast Margin,
    # which is private and binds 127.0.0.1.) The auth interlock, not the bind
    # address, is what protects the mutating endpoints.
    host: str = "0.0.0.0"
    port: int = 7777


_DEFAULT_CONFIG_PATH = Path.home() / "seren-observatory" / "seren-observatory.yaml"


def _resolve_config_path(explicit_path: Optional[str] = None) -> Optional[Path]:
    """--config -> $SEREN_AGENT_CONFIG -> ~/seren-observatory/seren-observatory.yaml -> None."""
    if explicit_path:
        return Path(explicit_path).expanduser()
    env = os.getenv("SEREN_AGENT_CONFIG")
    if env:
        return Path(env).expanduser()
    if _DEFAULT_CONFIG_PATH.exists():
        return _DEFAULT_CONFIG_PATH
    return None


def _load_yaml_lenient(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if not _HAS_YAML:
        print(f"[seren-observatory] config: pyyaml not installed; ignoring {path}")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        print(f"[seren-observatory] config: failed to parse {path}: {e} (using defaults)")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        print(f"[seren-observatory] config: {path} top-level must be a mapping; got "
              f"{type(data).__name__} (using defaults)")
        return {}
    return data


def _apply_server_overrides(cfg: ObservatoryConfig, server: dict[str, Any], *, source: str) -> None:
    """Apply per-key overrides; each key try/except'd so one bad value doesn't
    sink the others. Only host/port are known - anything else is ignored with
    a note (notably 'bearer_token', which is intentionally NOT honored here)."""
    known = {"host", "port"}
    for key, raw in server.items():
        if key == "bearer_token":
            # Loud, specific note: the token is not a config field by design.
            print("[seren-observatory] config: 'bearer_token' in the yaml is ignored "
                  "by design - the observatory token lives in ~/.seren/secrets.json "
                  "(run seren-secrets.sh). See config.py for why.")
            continue
        if key not in known:
            print(f"[seren-observatory] config: ignoring unknown server key '{key}' from {source}")
            continue
        try:
            current = cfg.model_dump()
            current[key] = raw
            cfg.__dict__.update(ObservatoryConfig.model_validate(current).__dict__)
        except Exception as e:
            print(f"[seren-observatory] config: ignored bad value for '{key}' from {source}: {e}")


def load_config(path: Optional[str] = None) -> ObservatoryConfig:
    """Defaults -> YAML (server: block) -> env vars. Never raises on bad input.

    ``path`` is the --config flag value (highest-priority config location).
    """
    cfg = ObservatoryConfig()

    # Layer 2: YAML
    yaml_path = _resolve_config_path(path)
    if yaml_path is not None:
        data = _load_yaml_lenient(yaml_path)
        server = data.get("server")
        if isinstance(server, dict):
            _apply_server_overrides(cfg, server, source=str(yaml_path))
        elif server is not None:
            print(f"[seren-observatory] config: 'server' in {yaml_path} must be a mapping; ignoring")

    # Layer 3: env vars (highest precedence). Honor BOTH the original
    # AGENT_HOST/AGENT_PORT (what app.py historically read, and what the old
    # launcher exported) AND the SEREN_AGENT_* aliases the yaml sample
    # documents. The SEREN_AGENT_* form wins if both are somehow set, since
    # it's the documented, namespaced one.
    env_overrides: dict[str, Any] = {}
    for env_key, attr in (("AGENT_HOST", "host"),
                          ("AGENT_PORT", "port"),
                          ("SEREN_AGENT_HOST", "host"),
                          ("SEREN_AGENT_PORT", "port")):
        v = os.getenv(env_key)
        if v is not None:
            env_overrides[attr] = v
    if env_overrides:
        _apply_server_overrides(cfg, env_overrides, source="environment")

    return cfg
