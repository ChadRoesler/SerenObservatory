"""
An upgrade step that RAISES (seren_observatory.upgrades.Upgrader._crashed).

19 Oct 2026, the nuc: a hippocampus upgrade ended "failed: PermissionError:
[Errno 1] Operation not permitted", no restart note, nothing rolled back,
while systemd showed the new version up since just after the install. The
record could not say what raised, and called a working service failed.

Pinned here:
- the crash note names the step that was running and the frame that raised
- the service is looked at: up on a process started after the install and
  answering its health check, the job is DONE with a warning and the ledger
  tells the new version
- still the old process, or not answering: the job stays failed, says which,
  and nothing is rolled back (as before - but now it says so)
- a crash before the install touched anything is a plain failure
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory import lifecycle, upgrades
from seren_observatory.app import create_app

from tests.test_upgrades import OBS, FakeBox, _finish, box  # noqa: F401 - the fixture


def _eperm():
    raise PermissionError(1, "Operation not permitted")


async def _upgrade(box):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"version": "5.0.1", "wait_seconds": 5},
                         headers=OBS)
        assert r.status_code == 202, r.text
        return await _finish(c, r.json()["job"]["id"])


async def test_a_restart_that_raised_after_the_service_came_up_is_done_with_a_warning(box, monkeypatch):
    box.on_restart = _eperm                                   # systemctl ran, then something raised

    async def status(manifest):                               # a young process: started after pip finished
        return {"running": True, "uptime_seconds": 0}
    monkeypatch.setattr(lifecycle, "status", status)

    job = await _upgrade(box)
    assert job["state"] == "done", job
    assert job["error"] is None
    assert "PermissionError: [Errno 1] Operation not permitted" in job["warning"]
    assert "the upgrade stands" in job["warning"]
    crash = next(s for s in job["steps"] if s["step"] == "crash")
    assert crash["during"] == "restart", "the step that was running, not the last one noted"
    assert "test_upgrade_crash.py" in crash["where"] and "_eperm" in crash["where"], "the frame that raised"
    assert crash["service"]["restarted"] is True and crash["service"]["healthy"] is True
    assert job["steps"][-1]["step"] == "done" and job["steps"][-1]["after_crash"] is True
    assert json.loads(box.ledger.read_text())["version"] == "5.0.1", "the ledger tells the truth"
    assert box.versions["seren-memory"] == "5.0.1", "nothing rolled back"


async def test_a_restart_that_raised_with_the_old_process_still_up_stays_failed_and_says_so(box, monkeypatch):
    box.on_restart = _eperm

    async def status(manifest):                               # an old process: up since long before the install
        return {"running": True, "uptime_seconds": 36_000}
    monkeypatch.setattr(lifecycle, "status", status)

    job = await _upgrade(box)
    assert job["state"] == "failed", job
    assert "PermissionError" in job["error"] and "(step restart)" in job["error"]
    assert "nothing was rolled back" in job["error"] and "running on the old process" in job["error"]
    assert job["warning"] is None
    crash = next(s for s in job["steps"] if s["step"] == "crash")
    assert crash["service"]["restarted"] is False
    assert box.versions["seren-memory"] == "5.0.1", "the new packages are on disk; the record says so"
    assert json.loads(box.ledger.read_text())["version"] == "5.0.0", "the ledger still names what runs"


async def test_a_restart_that_raised_and_a_service_not_answering_stays_failed(box, monkeypatch):
    box.on_restart = _eperm
    box.healthy_after_upgrade = False

    async def status(manifest):
        return {"running": False}
    monkeypatch.setattr(lifecycle, "status", status)

    job = await _upgrade(box)
    assert job["state"] == "failed"
    assert "the service is not answering" in job["error"]


async def test_a_crash_before_the_install_is_a_plain_failure(box, monkeypatch):
    async def boom(name, reason):
        raise RuntimeError("the store keeper is away")
    monkeypatch.setattr(upgrades, "take_snapshot", boom)

    job = await _upgrade(box)
    assert job["state"] == "failed"
    assert job["error"].startswith("RuntimeError: the store keeper is away in ")
    assert "(step snapshot)" in job["error"], "the step that was running when it raised"
    crash = next(s for s in job["steps"] if s["step"] == "crash")
    assert "service" not in crash, "nothing was installed, so there is nothing to look at"
    assert box.versions["seren-memory"] == "5.0.0"
