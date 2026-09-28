"""
windows_service: the Observatory drives a Windows box's NSSM services with
sc.exe, unprivileged, through a per-service grant the Starwright card writes.

Chad, 27 Sept 2026 (obs-win-manifests): nothing on Windows wrote a manifest, so
an Observatory there listed nothing it could start or stop. The PowerShell
service core now writes one and grants the Observatory's account start, stop
and query on that one service; these tests pin the Observatory's half.

sc.exe is a fake Service Control Manager that answers in sc's own words and
walks through the pending states, because sc start/stop return while the
service is still START_PENDING / STOP_PENDING:

  - start waits for RUNNING; a service already running is not started again;
  - stop waits for STOPPED; restart is a stop that finished, then a start;
  - a restart whose stop failed does not start;
  - access denied says how to put the grant back;
  - a start stuck in START_PENDING is an error, not an ok;
  - status reads the pid from sc queryex and probes the port;
  - reclaim leaves windows services alone by default, like the seren units.
"""
from __future__ import annotations

import subprocess

import pytest

from seren_observatory import lifecycle, manifests
from seren_observatory.system_routes import reclaim_plan

CODES = {"STOPPED": 1, "START_PENDING": 2, "STOP_PENDING": 3, "RUNNING": 4}


class FakeSCM:
    """Stands in for subprocess.run when argv[0] is sc.exe."""

    def __init__(self, state="STOPPED", *, pid=4242, deny=False, stuck=False, stop_rc=0):
        self.state, self.pid, self.deny, self.stuck, self.stop_rc = state, pid, deny, stuck, stop_rc
        self.calls: list[list[str]] = []

    def _queryex(self, name):
        pid = self.pid if self.state == "RUNNING" else 0
        return (f"\nSERVICE_NAME: {name}\n"
                f"        TYPE               : 10  WIN32_OWN_PROCESS\n"
                f"        STATE              : {CODES[self.state]}  {self.state}\n"
                f"                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)\n"
                f"        WIN32_EXIT_CODE    : 0  (0x0)\n"
                f"        PID                : {pid}\n"
                f"        FLAGS              :\n")

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append(argv)
        verb, name = argv[1], argv[2]
        done = lambda rc=0, out="": subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        if verb == "queryex":
            out = self._queryex(name)
            # Pending states settle on the next look, the way a real service does.
            if not self.stuck:
                self.state = {"START_PENDING": "RUNNING", "STOP_PENDING": "STOPPED"}.get(self.state, self.state)
            return done(out=out)
        if self.deny:
            return done(5, "[SC] StartService: OpenService FAILED 5:\n\nAccess is denied.")
        if verb == "start":
            if self.state == "RUNNING":
                return done(1056, "[SC] StartService FAILED 1056")
            self.state = "START_PENDING"
            return done()
        if verb == "stop":
            if self.stop_rc:
                return done(self.stop_rc, "[SC] ControlService FAILED")
            self.state = "STOP_PENDING"
            return done()
        raise AssertionError(f"unexpected sc verb {verb}")

    def verbs(self):
        return [c[1] for c in self.calls if c[1] != "queryex"]


M = {"schema_version": 2, "service": "seren-memory", "service_type": "windows_service",
     "windows_service": "seren-memory", "port": 7420,
     "health_url": "http://127.0.0.1:7420/health"}


@pytest.fixture
def scm(monkeypatch):
    def install(**kw):
        fake = FakeSCM(**kw)
        monkeypatch.setattr(lifecycle.subprocess, "run", fake)
        monkeypatch.setattr(lifecycle.time, "sleep", lambda s: None)
        return fake
    return install


def test_the_type_is_known():
    assert manifests.service_type(M) == "windows_service"
    assert manifests.service_has_lifecycle(M)


def test_start_waits_for_running(scm):
    fake = scm(state="STOPPED")
    res = lifecycle._start_sync(M)
    assert res["ok"] is True
    assert fake.verbs() == ["start"]
    assert fake.state == "RUNNING"


def test_a_running_service_is_not_started_again(scm):
    fake = scm(state="RUNNING")
    assert lifecycle._start_sync(M) == {"ok": True, "already_running": True}
    assert fake.verbs() == []


def test_stop_waits_for_stopped_and_restart_is_stop_then_start(scm):
    fake = scm(state="RUNNING")
    res = lifecycle._restart_sync(M)
    assert res["ok"] is True
    assert fake.verbs() == ["stop", "start"]
    assert res["stop"]["was_running"] is True


def test_a_restart_whose_stop_failed_does_not_start(scm):
    fake = scm(state="RUNNING", stop_rc=1051)
    res = lifecycle._restart_sync(M)
    assert res["ok"] is False and res["start"] is None
    assert fake.verbs() == ["stop"]


def test_access_denied_says_how_to_put_the_grant_back(scm):
    scm(state="STOPPED", deny=True)
    res = lifecycle._start_sync(M)
    assert res["ok"] is False
    assert res["error"] == "access denied"
    assert "Starwright card" in res["hint"]


def test_a_start_stuck_pending_is_an_error(scm, monkeypatch):
    scm(state="STOPPED", stuck=True)
    monkeypatch.setattr(lifecycle, "_WINDOWS_WAIT_S", 0.0)
    res = lifecycle._start_sync(M)
    assert res["ok"] is False
    assert "START_PENDING" in res["error"]


async def test_status_reads_the_pid_and_probes_the_port(scm, monkeypatch):
    scm(state="RUNNING", pid=5150)
    probed = []

    async def fake_probe(url, timeout=2.0):
        probed.append(url)
        return {"ok": True}
    monkeypatch.setattr(lifecycle, "_http_probe", fake_probe)
    st = await lifecycle.status(M)
    assert st["running"] is True and st["pid"] == 5150
    assert st["port_health"] == {"ok": True}
    assert probed == ["http://127.0.0.1:7420/health"]


async def test_status_of_a_stopped_service(scm):
    scm(state="STOPPED")
    st = await lifecycle.status(M)
    assert st["running"] is False and st["state"] == "STOPPED"


def test_off_windows_there_is_no_sc(monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(lifecycle.subprocess, "run", missing)
    res = lifecycle._sc(["queryex", "seren-memory"])
    assert res["ok"] is False and "only work on Windows" in res["error"]


def test_reclaim_leaves_windows_services_alone_by_default():
    fleet = {"seren-memory": M,
             "llama": {"service": "llama", "service_type": "pid_file", "port": 8090}}
    cands, kept = reclaim_plan(fleet, exclude=set(), include=set(), everything=False)
    assert [n for n, _ in cands] == ["llama"]
    assert "not a GPU daemon" in {k["service"]: k["why"] for k in kept}["seren-memory"]


def test_reclaim_all_never_stops_an_observatory_on_either_platform():
    fleet = {
        "obs-win": {"service": "obs-win", "service_type": "windows_service",
                    "windows_service": "seren-observatory", "port": 7777},
        "obs-wren": {"service": "obs-wren", "service_type": "systemd",
                     "systemd_unit": "seren-observatory-wren.service", "port": 7778},
        "seren-memory": M,
    }
    cands, kept = reclaim_plan(fleet, exclude=set(), include=set(), everything=True)
    assert [n for n, _ in cands] == ["seren-memory"]
    why = {k["service"]: k["why"] for k in kept}
    assert "never stops itself" in why["obs-win"] and "never stops itself" in why["obs-wren"]
