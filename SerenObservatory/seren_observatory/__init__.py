"""
seren-observatory - per-node management plane.

One runs on every node in the constellation - Jetson, Spark or NUC. The
hardware is not special to anything above it; what matters is that something
answers here.

HTTP API exposing manifest-driven discovery and lifecycle of locally-installed
Seren services (llama, kokoro, comfy, whisper, coral, and on a management node
the seren-* constellation services themselves). Consumed by:
    - SerenLodestar for cluster orchestration and chat-app workflows
      (this was SerenRuntimeHost, in C#, before the rename and the port)
    - The NUC dashboard for monitoring

Routes are versioned under /api/v1/. See service_routes.py and system_routes.py.
"""
from __future__ import annotations

# Version flows from the git tag via setuptools-scm (written to _version.py at
# build time, read here). Fallback only fires in a bare source checkout that was
# never built. Mirrors SerenLoci/SCC so the family exposes __version__ alike.
try:
    from ._version import version as __version__
except Exception:  # noqa: BLE001 - source checkout without a build
    __version__ = "0.0.0+unknown"
