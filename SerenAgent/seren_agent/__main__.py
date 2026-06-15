"""Entry point for `python -m seren_agent`.

Accepts --config / -c to match the SerenMemory convention (Memory leads, the
rest follow). Host/port come from the resolved config (which itself layers
defaults < yaml < env). The bearer token is NOT a config concern - it's loaded
separately from ~/.seren/secrets.json by auth.load_token(). See config.py.
"""
from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="seren_agent",
        description="seren-agent - per-node management plane.")
    parser.add_argument(
        "--config", "-c", default=None,
        help="Path to seren-agent.yaml (default: $SEREN_AGENT_CONFIG, then "
             "~/seren-agent/seren-agent.yaml, falling back to built-in "
             "defaults of 0.0.0.0:7777).")
    args = parser.parse_args()

    cfg = load_config(args.config)
    app = create_app(cfg)

    print(f"[seren-agent] listening on {cfg.host}:{cfg.port}")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
