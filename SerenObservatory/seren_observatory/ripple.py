"""
seren_observatory.ripple
════════════════════════════════════════════════════════════════════════

The receiving end of a ripple on a model's box.

WHY: the hippocampus asks the main model for a brief at bedtime and for a
review when drafts wait - a ripple, after the sharp-wave ripples a sleeping
hippocampus fires to reach the cortex. When both live on one box it runs the
command itself. When they don't - the hippocampus on a Jetson, the model on
someone's desktop - the hippocampus (or Lodestar,
routing it) POSTs the ripple here, and the Observatory starts the command AS
the person.

The running is seren_sinew.ripple's, shared with the hippocampus and
Lodestar: the command is this box's config, never the caller's; the message
fills {message} or goes on stdin; it runs as run_as and a LocalSystem / root
Observatory with no run_as refuses; one at a time per event; output in
~/seren-logs/ripple.log. What this module adds is the gate: off until
`ripple.enabled`, and like every POST here it needs the Observatory's bearer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from seren_sinew.ripple import RippleRunner as _Runner


class RippleRunner:
    def __init__(self, cfg, log_dir: Optional[Path] = None) -> None:
        self._cfg = cfg                     # RippleConfig
        self._log_dir = log_dir if log_dir is not None else Path.home() / "seren-logs"
        self._runner: Optional[_Runner] = None

    def _get(self) -> _Runner:
        if self._runner is None:
            c = self._cfg
            self._runner = _Runner(command=c.command, run_as=c.run_as, cwd=c.cwd,
                                   timeout_seconds=c.timeout_seconds, stdin=c.stdin,
                                   log_path=self._log_dir / "ripple.log")
        return self._runner

    def run(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """(http status, answer). Never raises."""
        if not self._cfg.enabled:
            return 409, {"ok": False, "error": "ripple is not set up on this node (ripple.enabled is false)"}
        return self._get().run(str(body.get("event") or "ripple"), str(body.get("message") or ""),
                               draft_id=str(body.get("draft_id") or ""), payload=body)

    def wait(self, timeout: float = 30.0) -> None:
        if self._runner is not None:
            self._runner.wait(timeout)
