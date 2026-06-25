"""DEPRECATED - moved to seren_sinew.request_log (the shared "one copy").

Observatory now mounts RequestLoggingMiddleware from seren-sinew (see app.py).
This module is a thin forwarder kept only so an in-flight import doesn't break
mid-migration. Nothing in Observatory imports it anymore - delete it on the
next push:

    git rm seren_observatory/request_log.py

Kept intentionally tiny: re-exports the names that used to live here so any
stray external import still resolves. No implementation lives here now.
"""
from seren_sinew.request_log import (  # noqa: F401
    RequestLoggingMiddleware,
    get_logger,
    setup_request_logger,
)
