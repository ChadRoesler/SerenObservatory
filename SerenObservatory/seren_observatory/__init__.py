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
    from ._version import version as __version__
except Exception:  # noqa: BLE001 - source checkout without a build
    __version__ = "0.0.0+unknown"
