"""
Upgrading a service through its node's Observatory (seren_observatory.upgrades):
snap it, snap its config, upgrade, check it, put it back.

pip, the restart and the health probe are replaced; everything else is real:
the ledger lookup, the job file, the config copies, the put-back. Pinned here:

- the install comes from the ledger record whose config is the manifest's;
  no record = not upgradable, said plainly (409), and nothing runs
- the happy path: snapshot, config held, pip names the package with its
  extras AND sinew + meninges (the 12 Oct 2026 lesson), preflight, restart,
  health, ledger version updated, job done
- pip fails: nothing restarted, versions untouched, job rolled_back
- health fails: the old versions pinned back, the config put back, restarted
- something changed the config: the copy goes back, what was there is kept
- a service that keeps stores and will not snapshot is not upgraded
- the Observatory upgrading itself hands the restart to what started it and
  the next Observatory closes the job: systemd on Linux; on Windows it proves
  NSSM is minding it with a Restart policy and exits, else rolls back; as a
  pid file or a launcher it refuses, nothing would start it again
- requests are checked before anything reaches pip; POST needs the token
- peers: a version that needs a newer Memory or Loci than the cluster runs is
  held before anything is touched (pip's dry run says what it needs); the
  installed metadata is checked again after pip, and rolls back; an app not
  in the cluster is not required; the reader's own instance is the one that
  counts; the versions route is what Lodestar collects
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory import lifecycle, upgrades
from seren_observatory.app import create_app

OBS = {"Authorization": "Bearer obs-secret"}
WINDOWS_START = 1 if os.name == "nt" else 0


class FakeBox:
    """pip, the venv's versions, the restarts and health, as the job sees them."""

    def __init__(self):
        self.versions = {"seren-memory": "5.0.0", "seren-sinew": "1.10.0", "seren-meninges": "2.6.0"}
        self.pip_calls: list[list[str]] = []
        self.restarts = 0
        self.pip_fails = False
        self.import_fails = False
        self.healthy_after_upgrade = True
        self.on_restart = None
        self.requires = ["seren-sinew>=5.0"]          # seren-memory's Requires-Dist, installed
        self.new_requires = None                      # the release pip's dry run sees; None: not moving
        self.requires_after = None                    # what the install really brought; None: new_requires

    def run(self, argv, timeout):
        if argv[1] == "-c" and argv[2] == upgrades._VERSIONS:
            return {"ok": True, "exit_code": 0, "out": json.dumps({d: self.versions.get(d) for d in argv[3:]})}
        if argv[1] == "-c" and argv[2] == upgrades._REQUIRES:
            return {"ok": True, "exit_code": 0, "out": json.dumps(self.requires if argv[3] == "seren-memory" else [])}
        if argv[1] == "-c" and argv[2].startswith("import "):
            return {"ok": not self.import_fails, "exit_code": 1 if self.import_fails else 0,
                    "out": "ImportError: boom" if self.import_fails else ""}
        if argv[1:4] == ["-m", "pip", "install"]:
            self.pip_calls.append(argv)
            specs = [a for a in argv[4:] if not a.startswith("-")]
            if "--dry-run" in argv:
                rep = {"install": [{"metadata": {"name": "seren-sinew", "version": "5.0.0"}}]}
                if self.new_requires is not None:
                    rep["install"].append({"metadata": {"name": "seren-memory", "version": "5.1.0",
                                                        "requires_dist": self.new_requires}})
                return {"ok": True, "exit_code": 0, "out": json.dumps(rep)}
            if self.pip_fails and "--upgrade" in argv:
                return {"ok": False, "exit_code": 1, "out": "ERROR: ResolutionImpossible"}
            for s in specs:
                name, _, ver = s.partition("==")
                name = re.sub(r"\[.*\]", "", name)
                self.versions[name] = ver or "9.9.9"
            if "--upgrade" in argv:
                after = self.requires_after if self.requires_after is not None else self.new_requires
                if after is not None:
                    self.before_requires, self.requires = self.requires, after
            elif getattr(self, "before_requires", None) is not None:
                self.requires = self.before_requires          # the rollback
            return {"ok": True, "exit_code": 0, "out": "Successfully installed"}

    def installs(self):
        return [a for a in self.pip_calls if "--upgrade" in a and "--dry-run" not in a]
        raise AssertionError(f"unexpected command {argv}")

    async def restart(self, manifest):
        self.restarts += 1
        if self.on_restart:
            self.on_restart()
        return {"ok": True}

    async def stop(self, manifest):
        return {"ok": True}

    async def probe(self, manifest):
        upgraded = self.versions["seren-memory"] != "5.0.0"
        return {"ok": self.healthy_after_upgrade or not upgraded}


@pytest.fixture()
def box(fake_home, tmp_path, monkeypatch):
    root = tmp_path / "root"
    app_dir = root / "apps" / "memory"
    app_dir.mkdir(parents=True)
    (app_dir / "seren-memory.yaml").write_text("server:\n  port: 7250\nsleep:\n  at: '02:00'\n")
    (app_dir / "seren-memory.env").write_text("SEREN_X=1\n")
    venv = root / "venvs" / "memory"
    for rel in ("bin/python", "Scripts/python.exe"):
        (venv / rel).parent.mkdir(parents=True, exist_ok=True)
        (venv / rel).write_text("")
    svc = fake_home / ".seren" / "services"
    (svc / "seren-memory-wren.json").write_text(json.dumps({
        "schema_version": 2, "service": "seren-memory-wren", "service_type": "systemd",
        "systemd_unit": "seren-memory-wren.service", "port": 7250,
        "config_path": str(app_dir / "seren-memory.yaml"), "app_dir": str(app_dir),
        "health_url": "http://127.0.0.1:7250/health"}))
    (svc / "llama.json").write_text(json.dumps({
        "schema_version": 2, "service": "llama", "service_type": "pid_file", "port": 8090}))
    ledger = fake_home / ".seren" / "installed"
    ledger.mkdir()
    (ledger / "seren-memory@wren.json").write_text(json.dumps({
        "service": "seren-memory", "instance": "wren", "package": "seren-memory", "version": "5.0.0",
        "venv": str(venv), "config": str(app_dir / "seren-memory.yaml"), "app_dir": str(app_dir),
        "extras": {"mcp": True, "corp": False, "st": True}}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    monkeypatch.delenv("SEREN_INSTALLED_DIR", raising=False)
    b = FakeBox()
    b.app_dir, b.ledger = app_dir, ledger / "seren-memory@wren.json"
    monkeypatch.setattr(upgrades, "_run", b.run)
    monkeypatch.setattr(lifecycle, "restart", b.restart)
    # On Windows a service is stopped before pip and started after it: the
    # start is that platform's restart, and counts as one.
    monkeypatch.setattr(lifecycle, "start", b.restart)
    monkeypatch.setattr(lifecycle, "stop", b.stop)
    monkeypatch.setattr(lifecycle, "probe_port", b.probe)
    monkeypatch.setattr(upgrades, "HEALTH_POLL_S", 0.01)

    async def snapshot(name, reason):
        b.snapshot_reason = reason
        return {"ok": True, "id": "20261012-0100", "snapshot": {"id": "20261012-0100"}}
    monkeypatch.setattr(upgrades, "take_snapshot", snapshot)
    return b


async def _finish(c, job_id, tries=500):
    for _ in range(tries):
        j = (await c.get(f"/api/v1/upgrades/{job_id}", headers=OBS)).json()["job"]
        if j["terminal"] or j["state"] == "restarting":
            return j
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished: {j}")


async def test_the_happy_path_snaps_upgrades_and_moves_the_libraries(box):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"version": "5.0.1", "wait_seconds": 5},
                         headers=OBS)
        assert r.status_code == 202, r.text
        job = await _finish(c, r.json()["job"]["id"])
    assert job["state"] == "done", job
    install = box.installs()[0]
    assert "seren-memory[mcp,st]==5.0.1" in install, "the extras ride along"
    assert "seren-sinew" in install and "seren-meninges" in install, "the libraries are named, so they move"
    assert job["before"] == {"seren-memory": "5.0.0", "seren-sinew": "1.10.0", "seren-meninges": "2.6.0"}
    assert job["after"]["seren-memory"] == "5.0.1" and job["after"]["seren-sinew"] == "9.9.9"
    assert job["snapshot"]["id"] == "20261012-0100" and box.restarts == 1
    held = {Path(c["path"]).name for c in job["config"]}
    assert held == {"seren-memory.yaml", "seren-memory.env"}
    assert all(Path(c["copy"]).is_file() for c in job["config"])
    assert [s["step"] for s in job["steps"]] == ["plan", "peers", "snapshot", "config", "install", "preflight",
                                                  "restart", "put_back", "done"]
    assert json.loads(box.ledger.read_text())["version"] == "5.0.1", "the ledger tells the truth after"


async def test_pip_failing_restarts_nothing_and_changes_nothing(box):
    box.pip_fails = True
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"version": "5.0.1"}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
    assert job["state"] == "rolled_back" and job["error"] == "pip failed"
    # Linux: never stopped, so nothing to restart. Windows stopped it for pip,
    # so it is started again, on the old versions.
    assert box.restarts == WINDOWS_START and box.versions["seren-memory"] == "5.0.0"
    assert "ResolutionImpossible" in " ".join(job["pip"])
    assert json.loads(box.ledger.read_text())["version"] == "5.0.0"


async def test_a_version_that_will_not_import_is_rolled_back_before_any_restart(box):
    box.import_fails = True
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"version": "5.0.1"}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
    assert job["state"] == "rolled_back" and box.restarts == WINDOWS_START
    assert box.versions == {"seren-memory": "5.0.0", "seren-sinew": "1.10.0", "seren-meninges": "2.6.0"}
    back = box.pip_calls[-1]
    assert "seren-memory[mcp,st]==5.0.0" in back and "seren-sinew==1.10.0" in back


async def test_no_health_after_the_upgrade_rolls_it_all_back(box):
    box.healthy_after_upgrade = False
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade",
                         json={"version": "5.0.1", "wait_seconds": 5}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"], tries=2000)
    assert job["state"] == "rolled_back", job
    assert box.versions["seren-memory"] == "5.0.0" and box.restarts == 2, "restarted on the old versions"
    assert "healthy" in job["error"]


async def test_a_config_something_changed_is_put_back_and_the_change_kept(box):
    cfg = box.app_dir / "seren-memory.yaml"
    original = cfg.read_text()
    box.on_restart = lambda: cfg.write_text("server:\n  port: 7250\n") if box.restarts == 1 else None
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade",
                         json={"version": "5.0.1", "wait_seconds": 5}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
    assert job["state"] == "done"
    assert cfg.read_text() == original, "the operator's config wins"
    put = next(s for s in job["steps"] if s["step"] == "put_back")
    assert put["files"] == [str(cfg)] and box.restarts == 2
    copy = Path(next(c["copy"] for c in job["config"] if c["path"] == str(cfg)))
    assert copy.with_name(copy.name + ".from-upgrade").read_text() == "server:\n  port: 7250\n"


async def test_a_service_that_will_not_snapshot_is_not_upgraded(box, monkeypatch):
    async def refuse(name, reason):
        return {"ok": False, "error": "disk full"}
    monkeypatch.setattr(upgrades, "take_snapshot", refuse)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
        assert job["state"] == "failed" and "disk full" in job["error"]
        assert not box.installs()
        # Lodestar, having pulled a copy off the box, skips it
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"snapshot": False}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
    assert job["state"] == "done" and job["snapshot"] is None
    assert "seren-memory[mcp,st]" in box.installs()[0], "no version = the newest"


async def test_dry_run_is_pips_plan_and_changes_nothing(box):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        p = (await c.post("/api/v1/service/seren-memory-wren/upgrade",
                          json={"version": "5.0.1", "dry_run": True}, headers=OBS)).json()
        assert (await c.get("/api/v1/upgrades", headers=OBS)).json()["jobs"] == []
    assert p["ok"] and p["dry_run"] and p["upgradable"] and p["spec"] == "seren-memory[mcp,st]==5.0.1"
    assert p["would_install"] == [{"name": "seren-sinew", "version": "5.0.0"}]
    assert p["before"]["seren-memory"] == "5.0.0" and box.restarts == 0
    assert "--dry-run" in box.pip_calls[0]


async def test_what_pip_was_not_used_for_is_not_upgradable(box):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/llama/upgrade", json={}, headers=OBS)
        assert r.status_code == 409 and r.json()["upgradable"] is False
        assert (await c.post("/api/v1/service/nope/upgrade", json={}, headers=OBS)).status_code == 404
    assert box.pip_calls == []


@pytest.mark.parametrize("body", [
    {"version": "--index-url=http://evil"}, {"version": "5.0.1; rm -rf /"},
    {"with": ["requests"]}, {"with": ["--pre"]}, {"with": ["seren-sinew @ http://x"]}, {"with": "seren-sinew"},
])
async def test_nothing_reaches_pip_that_is_not_a_version_or_a_seren_package(box, body):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json=body, headers=OBS)
    assert r.status_code == 400 and box.pip_calls == []


async def test_the_interlock_and_one_at_a_time(box, monkeypatch):
    gate = asyncio.Event()
    real = upgrades.Upgrader._run

    async def slow(self, *a, **k):
        await gate.wait()
        return await real(self, *a, **k)
    monkeypatch.setattr(upgrades.Upgrader, "_run", slow)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/api/v1/service/seren-memory-wren/upgrade", json={})).status_code in (401, 403)
        first = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={}, headers=OBS)
        await asyncio.sleep(0.05)
        second = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={}, headers=OBS)
        assert first.status_code == 202 and second.status_code == 409
        gate.set()
        await _finish(c, first.json()["job"]["id"])


async def test_the_observatory_upgrading_itself_hands_the_restart_to_systemd(box, fake_home, monkeypatch):
    ledger = json.loads(box.ledger.read_text())
    ledger["venv"] = sys.prefix                       # this very Observatory
    box.ledger.write_text(json.dumps(ledger))
    monkeypatch.setattr(upgrades, "venv_python", lambda v: sys.executable)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"version": "5.0.1"}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
    assert job["state"] == "restarting" and job["self_upgrade"] and box.restarts == 1
    # the next Observatory starts, reads the job and closes it on the version that came up
    monkeypatch.setattr("importlib.metadata.version", lambda d: "5.0.1")
    assert upgrades.finish_self_upgrades() == [job["id"]]
    assert upgrades.load(job["id"]).state == "done"


def _itself_as(box, fake_home, monkeypatch, service_type, **more):
    """The ledger says the service is this very Observatory; the manifest
    says how it is run."""
    ledger = json.loads(box.ledger.read_text())
    ledger["venv"] = sys.prefix
    box.ledger.write_text(json.dumps(ledger))
    monkeypatch.setattr(upgrades, "venv_python", lambda v: sys.executable)
    m = fake_home / ".seren" / "services" / "seren-memory-wren.json"
    manifest = json.loads(m.read_text())
    manifest.pop("systemd_unit", None)
    manifest.update(service_type=service_type, **more)
    m.write_text(json.dumps(manifest))


def _nssm(monkeypatch, policy="Restart", ppid=None, state="RUNNING"):
    """What sc.exe and the registry would say, and the exit as a record."""
    pid = os.getppid() if ppid is None else ppid
    monkeypatch.setattr(lifecycle, "_sc_query", lambda name: (state, pid))
    monkeypatch.setattr(upgrades, "nssm_exit_policy", lambda name: {"policy": policy, "source": "registry"})
    left = []
    monkeypatch.setattr(upgrades, "_exit_process", lambda: left.append(True))
    monkeypatch.setattr(upgrades, "EXIT_GRACE_S", 0.0)
    return left


async def test_on_windows_the_observatory_proves_nssm_is_minding_it_and_leaves(box, fake_home, monkeypatch):
    _itself_as(box, fake_home, monkeypatch, "windows_service", windows_service="SerenObservatory-wren")
    left = _nssm(monkeypatch)
    stops = []

    async def stop(manifest):
        stops.append(manifest["service"])
        return {"ok": True}
    monkeypatch.setattr(lifecycle, "stop", stop)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"version": "5.0.1"}, headers=OBS)
        assert r.status_code == 202, r.json()
        job = await _finish(c, r.json()["job"]["id"])
        await asyncio.sleep(0.05)
    assert job["state"] == "restarting" and job["self_upgrade"]
    restart = [n for n in job["steps"] if n["step"] == "restart"][0]
    assert restart["handed_to"] == "nssm" and restart["service"] == "SerenObservatory-wren"
    assert restart["exit_policy"]["policy"] == "Restart"
    # not stopped before pip (that would end the upgrade itself), not sc-restarted
    # after it (the stop would kill this process and nothing would run the start):
    # the new files went in under the running process, and then it left
    assert stops == [] and box.restarts == 0 and left == [True]
    assert box.versions["seren-memory"] == "5.0.1"
    # the next Observatory starts, reads the job and closes it on the version that came up
    monkeypatch.setattr("importlib.metadata.version", lambda d: "5.0.1")
    assert upgrades.finish_self_upgrades() == [job["id"]]
    assert upgrades.load(job["id"]).state == "done"


async def test_an_nssm_that_would_not_start_it_again_means_a_rollback_not_an_exit(box, fake_home, monkeypatch):
    _itself_as(box, fake_home, monkeypatch, "windows_service", windows_service="SerenObservatory-wren")
    left = _nssm(monkeypatch, policy="Exit")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"version": "5.0.1"}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
        await asyncio.sleep(0.05)
    assert job["state"] == "rolled_back" and "exit policy is Exit" in job["error"]
    assert left == [] and box.restarts == 0, "the old code is still running; leaving would have killed it"
    assert box.versions["seren-memory"] == "5.0.0", "the packages went back"


def test_the_handoff_check_on_its_own(monkeypatch):
    m = {"service": "seren-observatory-wren", "service_type": "windows_service",
         "windows_service": "SerenObservatory-wren"}
    _nssm(monkeypatch)
    assert upgrades.nssm_handoff_check(m)["ok"]
    # someone else's process is the service: exiting would restart nothing
    _nssm(monkeypatch, ppid=os.getppid() + 100000)
    out = upgrades.nssm_handoff_check(m)
    assert not out["ok"] and "not SerenObservatory-wren's program" in out["error"]
    _nssm(monkeypatch, state="STOPPED")
    assert "not RUNNING" in upgrades.nssm_handoff_check(m)["error"]
    assert upgrades.nssm_handoff_check({"service": "x", "service_type": "windows_service"})["error"] \
        == "manifest missing windows_service"
    # the registry read itself: a real key or NSSM's default on Windows, honest off it
    monkeypatch.undo()
    pol = upgrades.nssm_exit_policy("SerenNoSuchService-zz9")
    if os.name == "nt":
        assert pol == {"policy": "Restart", "source": "nssm default (no AppExit key)"}
    else:
        assert "not Windows" in pol["error"]


async def test_run_from_a_pid_file_the_observatory_still_refuses_to_upgrade_itself(box, fake_home, monkeypatch):
    _itself_as(box, fake_home, monkeypatch, "pid_file", pid_path=str(fake_home / "obs.pid"))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={}, headers=OBS)
    assert r.status_code == 409 and "Starwright" in r.json()["error"]
    assert box.installs() == []


# ── peers ────────────────────────────────────────────────────────────────────

NEEDS_LOCI = ["seren-sinew>=5.0", 'seren-loci>=5.2; extra == "peers"', 'sentence-transformers; extra == "st"']


def _peers(loci="5.1.0", **more):
    out = [{"service": "seren-loci-wren", "node": "nuc", "package": "seren-loci", "version": loci},
           {"service": "seren-memory-wren", "node": "nuc", "package": "seren-memory", "version": "5.0.0"}]
    return out + [{"service": k, "node": "jetson", "package": "seren-loci", "version": v} for k, v in more.items()]


def test_the_peer_check_on_its_own():
    c = upgrades.check_peers("seren-memory-wren", "seren-memory", "5.1.0", NEEDS_LOCI, _peers("5.1.0"))
    assert c["ok"] is False and c["needs"] == ["seren-loci>=5.2"], "only the peers extra counts"
    assert c["problems"] == ["seren-memory 5.1.0 needs seren-loci>=5.2; seren-loci-wren on nuc is 5.1.0"]
    assert upgrades.check_peers("seren-memory-wren", "seren-memory", "5.1.0", NEEDS_LOCI, _peers("5.2.0"))["ok"]
    gone = upgrades.check_peers("seren-memory-wren", "seren-memory", "5.1.0", NEEDS_LOCI, _peers()[1:])
    assert gone["ok"] and gone["unchecked"] == ["seren-loci>=5.2: none in the cluster"], "pick and choose"
    # another instance's old Loci is not this Memory's Loci
    mixed = _peers("5.2.0", **{"seren-loci-alice": "4.0.0"})
    assert upgrades.check_peers("seren-memory-wren", "seren-memory", "5.1.0", NEEDS_LOCI, mixed)["ok"]
    assert not upgrades.check_peers("seren-memory-bob", "seren-memory", "5.1.0", NEEDS_LOCI, mixed)["ok"], \
        "no Loci of its own: every one there counts"


async def test_a_version_that_needs_a_newer_peer_is_held_and_nothing_is_touched(box):
    box.new_requires = NEEDS_LOCI
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        p = (await c.post("/api/v1/service/seren-memory-wren/upgrade",
                          json={"dry_run": True, "peers": _peers()}, headers=OBS)).json()
        assert p["held"] and "seren-loci-wren on nuc is 5.1.0" in p["needs"]["problems"][0]
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"peers": _peers()}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
        assert job["state"] == "held" and "seren-loci>=5.2" in job["error"]
        assert not box.installs() and box.restarts == 0 and job["snapshot"] is None
        assert [s["step"] for s in job["steps"]] == ["plan", "peers"]
        # Loci moves; now it goes
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"peers": _peers("5.2.0")}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
    assert job["state"] == "done" and job["needs"]["needs"] == ["seren-loci>=5.2"]
    assert box.versions["seren-memory"] == "9.9.9"


async def test_what_the_install_really_needs_is_checked_again(box):
    box.new_requires = ["seren-sinew>=5.0"]
    box.requires_after = NEEDS_LOCI
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"peers": _peers()}, headers=OBS)
        job = await _finish(c, r.json()["job"]["id"])
    assert job["state"] == "rolled_back" and "too old" in job["error"]
    assert box.versions["seren-memory"] == "5.0.0" and box.restarts == WINDOWS_START


async def test_asked_by_hand_the_peers_are_this_nodes_own(box):
    box.new_requires = ['seren-memory>=9; extra == "peers"']        # needs a newer itself: absurd, but local
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        p = (await c.post("/api/v1/service/seren-memory-wren/upgrade", json={"dry_run": True}, headers=OBS)).json()
        v = (await c.get("/api/v1/upgrades/versions", headers=OBS)).json()
        assert (await c.post("/api/v1/service/seren-memory-wren/upgrade",
                             json={"peers": [{"service": 3}]}, headers=OBS)).status_code == 400
    assert p["held"] and "seren-memory-wren is 5.0.0" in p["needs"]["problems"][0]
    assert v["services"] == {"seren-memory-wren": {"package": "seren-memory", "version": "5.0.0", "needs": [],
                                                   "self": False}}, "llama: not a pip install, not listed"
