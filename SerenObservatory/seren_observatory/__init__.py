"""
seren-observatory - per-Jetson management plane.

HTTP API exposing manifest-driven discovery and lifecycle of locally-installed
Seren services (llama, kokoro, comfy, whisper, coral). Consumed by:
    - SerenRuntimeHost (C#) for chat-app workflows
    - SerenCommandCenter (SCC, future) for cluster orchestration
    - The NUC dashboard for monitoring

Routes are versioned under /api/v1/. See service_routes.py and system_routes.py.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("seren-observatory")
except PackageNotFoundError:
    # Running from a source checkout without an editable install.
    # Set SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 and run:
    #   pip install -e ".[dev]"
    # to resolve this.
    __version__ = "0.0.0.dev"
