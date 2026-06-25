"""Request logging for the observatory.

Logs every HTTP request with method, path, status, duration, and IP.
500s include the full traceback. Output goes to BOTH stderr (so journalctl
catches it) AND a rotating file at ~/seren-logs/observatory-requests.log so you
can read the log without sudo.

Why we don't lean on uvicorn's access log:
  - uvicorn's access log goes to stderr only (no file)
  - Format is fixed (no duration ms, no traceback on 5xx)
  - When journalctl needs sudo, you're stuck

Why log to a file the user owns:
  - 'sudo journalctl -u seren-observatory' requires a password by default
  - Anyone debugging needs the log NOW, not "go set up sudoers properly"
  - Same path as other service logs (~/seren-logs/) - consistent UX
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import time
import traceback
from pathlib import Path

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


def _setup_logger() -> logging.Logger:
    """Configure the request logger. Called once at app startup.

    Sets up two handlers:
      1. StreamHandler → stderr (so journalctl picks it up)
      2. RotatingFileHandler → ~/seren-logs/observatory-requests.log
         (so users can tail without sudo)

    Defaults to INFO. Set SEREN_AGENT_LOG_LEVEL=DEBUG to bump verbosity
    (DEBUG also dumps response body summaries for non-binary content).
    """
    logger = logging.getLogger("seren-observatory.requests")
    if logger.handlers:
        return logger  # already configured (avoid duplicate handlers on reload)

    level_name = os.environ.get("SEREN_AGENT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False  # don't double-log via root

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Stream → stderr
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    # File → ~/seren-logs/observatory-requests.log, daily rotation, 7 days kept
    log_dir = Path.home() / "seren-logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=log_dir / "observatory-requests.log",
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as e:
        # Don't crash the observatory if the log dir isn't writable - fall back
        # to stderr-only. Caller will still see request lines via journalctl.
        logger.warning(f"could not open file log at {log_dir}: {e}")

    return logger


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs every request with timing + status. Captures 500 tracebacks.

    Goes BEFORE auth in the middleware stack so we see auth-rejected
    requests too (those help debug "why is the dashboard 401ing").
    """

    def __init__(self, app):
        super().__init__(app)
        self._log = _setup_logger()

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        method = request.method
        path = request.url.path
        # Skip query string from logs by default - tokens or PII could leak.
        # If you need it for debug, set SEREN_AGENT_LOG_QUERY=1 in the env.
        if os.environ.get("SEREN_AGENT_LOG_QUERY") == "1" and request.url.query:
            path = f"{path}?{request.url.query}"

        client = request.client.host if request.client else "?"

        try:
            response = await call_next(request)
            duration_ms = int((time.perf_counter() - start) * 1000)
            status = response.status_code

            # Pick a log level based on status - INFO for 2xx/3xx, WARNING
            # for 4xx, ERROR for 5xx. Slow-request warning at >1s regardless.
            line = f"{client} {method} {path} → {status} ({duration_ms}ms)"
            if status >= 500:
                self._log.error(line)
            elif status >= 400:
                self._log.warning(line)
            elif duration_ms > 1000:
                self._log.warning(f"{line} [slow]")
            else:
                self._log.info(line)

            return response

        except Exception as e:
            # Unhandled exception escaped the route. Log full traceback
            # then re-raise so FastAPI's default 500 handler runs.
            duration_ms = int((time.perf_counter() - start) * 1000)
            tb = traceback.format_exc()
            self._log.error(
                f"{client} {method} {path} → 500 EXCEPTION ({duration_ms}ms)\n"
                f"  {type(e).__name__}: {e}\n{tb}"
            )
            raise


def get_logger() -> logging.Logger:
    """Public accessor for other modules that want to log to the same sink."""
    return _setup_logger()
