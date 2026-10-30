"""
An Observatory restarting ITSELF under systemd queues the restart instead
of waiting on it (lifecycle._systemd_restart no_block, upgrades self branch,
the restart route).

The nuc, 29 Oct 2026, job 20261029-014652-4301fd: the self-upgrade installed
5.7.0, handed the restart to systemd, and 15 ms later recorded 'restart
refused' and rolled back. The journal: sudo systemctl restart RAN, systemd
began stopping the unit, and its stop sent SIGTERM to the whole cgroup,
which included the systemctl that had asked. It came back -15 with nothing on
stderr, the job called that refused, pip put 5.6.0 back, and the old version
came up. Pinned here:

- the self-upgrade asks for a detached restart, and detached means
  `systemctl --no-block restart`
- a systemctl killed by its own stop is named as such in the job, not
  'restart refused'
- POST /service/<itself>/restart under systemd queues it and says so
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory import lifecycle, upgrades
from seren_observatory.app import create_app

from tests.test_upgrades import OBS, _finish, box  # noqa: F401 - the fixture

UNIT = {"service": "seren-observatory-wren", "service_type": "systemd",
        "systemd_unit": "seren-observatory-wren.service", "port": 7777}


def test_detached_means_no_block_except_through_the_node_helper(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(lifecycle, "_systemd_run", lambda argv, timeout=10.0: seen.append(argv) or {"ok": True})
    monkeypatch.setattr(lifecycle, "SEREN_SYSTEMCTL", str(tmp_path / "not-there"))
    lifecycle._restart_sync(UNIT, detach=True)
    lifecycle._restart_sync(UNIT)
    # A node with the sudo helper: one verb, one unit, no flag - and no need
    # for one, since its units run with KillMode=process.
    helper = tmp_path / "seren-systemctl"
    helper.write_text("")
    monkeypatch.setattr(lifecycle, "SEREN_SYSTEMCTL", str(helper))
    lifecycle._restart_sync(UNIT, detach=True)
    assert seen == [["systemctl", "--no-block", "restart", "seren-observatory-wren.service"],
                    ["systemctl", "restart", "seren-observatory-wren.service"],
                    ["systemctl", "restart", "seren-observatory-wren.service"]]


def _itself(box, monkeypatch):
    ledger = json.loads(box.ledger.read_text())
    ledger["venv"] = sys.prefix                       # this very Observatory
    box.ledger.write_text(json.dumps(ledger))
    monkeypatch.setattr(upgrades, "venv_python", lambda v: sys.executable)


async def test_the_self_upgrade_asks_for_a_detached_restart(box, fake_home, monkeypatch):
    _itself(box, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"version": "5.0.1"}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
    assert job["state"] == "restarting" and box.restarts == 1
    assert box.detached is True, "queued, so the stop it asks for cannot kill the asking"


async def test_a_systemctl_killed_by_its_own_stop_is_named_not_called_refused(box, fake_home, monkeypatch):
    _itself(box, monkeypatch)

    async def killed(manifest, detach=False):
        return {"ok": False, "exit_code": -15, "stdout": "", "stderr": ""}
    monkeypatch.setattr(lifecycle, "restart", killed)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"version": "5.0.1"}, headers=OBS)
        job_id = r.json()["job"]["id"]
        # _finish stops at "restarting", which a self-upgrade passes through on
        # its way to the rollback; wait for the terminal state itself.
        for _ in range(500):
            job = (await c.get(f"/api/v1/upgrades/{job_id}", headers=OBS)).json()["job"]
            if job["terminal"]:
                break
            await asyncio.sleep(0.01)
    assert job["state"] == "rolled_back", job
    note = [s for s in job["steps"] if s["step"] == "restart" and not s["ok"]][0]
    assert "killed by the stop it asked for" in note["error"] and note["exit_code"] == -15
    assert "cgroup" in job["error"] and "refused" not in job["error"]


async def test_the_restart_route_queues_a_restart_of_itself(fake_home, monkeypatch):
    (fake_home / ".seren" / "services" / "seren-observatory-wren.json").write_text(json.dumps(UNIT))
    (fake_home / ".seren" / "services" / "llama.json").write_text(json.dumps(
        {"service": "llama", "service_type": "systemd", "systemd_unit": "llama.service", "port": 8080}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    seen = []

    async def restart(manifest, detach=False):
        seen.append((manifest["service"], detach))
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(lifecycle, "restart", restart)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        me = (await c.post("/api/v1/service/seren-observatory-wren/restart", headers=OBS)).json()
        other = (await c.post("/api/v1/service/llama/restart", headers=OBS)).json()
    assert me["ok"] and me["handed_to"] == "systemd" and "queued" in me["note"]
    assert other == {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
    assert seen == [("seren-observatory-wren", True), ("llama", False)]
