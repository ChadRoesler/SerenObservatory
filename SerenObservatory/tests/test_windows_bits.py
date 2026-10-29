"""
What the Observatory reads off Win32 where a node reads /proc
(seren_observatory.winproc), and the Windows shapes of the probe, the port
wait and the reboot.

The reason (28 Oct 2026): a Windows box with its firewall up DROPS a SYN to a
closed port, loopback included, so a probe of a down service waited its
whole timeout, three URLs per status, and the error string was '' (a
ConnectTimeout's str). And a Windows service's memory, CPU and uptime, and
the box's own memory and uptime, were None: the desktop's card showed
dashes next to every node's numbers.

The ctypes tests run only on Windows (they ask the real kernel); the shape
tests run everywhere.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time

import pytest

from seren_observatory import lifecycle, winproc

windows_only = pytest.mark.skipif(os.name != "nt", reason="asks Win32 for real")


@pytest.fixture()
def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        yield s.getsockname()[1]
    finally:
        s.close()


@windows_only
def test_the_tcp_table_says_who_listens(listener):
    assert os.getpid() in winproc.listening_pids(listener)
    assert winproc.port_is_listening(listener) is True
    free = socket.socket(); free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]; free.close()
    assert winproc.port_is_listening(port) is False


@windows_only
async def test_a_probe_of_a_closed_local_port_answers_at_once_and_says_why():
    free = socket.socket(); free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]; free.close()
    t0 = time.perf_counter()
    r = await lifecycle._http_probe(f"http://127.0.0.1:{port}/health", timeout=2.0)
    assert time.perf_counter() - t0 < 1.0, "no waiting out the firewall's dropped SYN"
    assert r["ok"] is False and r["error"] == f"nothing is listening on port {port}"


@windows_only
async def test_a_probe_of_a_live_local_port_still_probes(listener):
    """Something listens but speaks no HTTP: the probe goes ahead and the
    error names what happened rather than ''."""
    r = await lifecycle._http_probe(f"http://127.0.0.1:{listener}/health", timeout=0.5)
    assert r["ok"] is False and r["error"], r


@windows_only
def test_the_port_wait_asks_the_table_not_a_bind(listener):
    """A bind with SO_REUSEADDR succeeds on Windows against a port someone
    listens on, so the old wait always said released."""
    t0 = time.perf_counter()
    assert lifecycle._wait_for_port_release(listener, timeout_s=0.6) is False
    assert 0.5 < time.perf_counter() - t0 < 3.0
    free = socket.socket(); free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]; free.close()
    assert lifecycle._wait_for_port_release(port, timeout_s=0.6) is True


@windows_only
def test_a_process_and_its_tree_have_memory_and_uptime():
    me = winproc.process_stats(os.getpid(), tree=False)
    assert me["memory_mb"] and me["memory_mb"] > 0
    assert me["uptime_seconds"] is not None and 0 <= me["uptime_seconds"] < 24 * 3600
    assert me["pids"] == [os.getpid()]
    child = subprocess.Popen([os.sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        time.sleep(0.3)
        tree = winproc.process_stats(os.getpid())
        assert child.pid in tree["pids"], "a child counts toward the service"
        assert tree["memory_mb"] >= me["memory_mb"]
    finally:
        child.kill()


@windows_only
async def test_cpu_percent_is_a_number_on_one_cores_scale():
    pct = await winproc.cpu_percent(os.getpid(), sample_ms=50)
    assert pct is not None and 0.0 <= pct <= 100.0 * (os.cpu_count() or 1)


@windows_only
def test_the_box_has_memory_and_uptime():
    rt = winproc.system_runtime()
    assert rt["memory_mb_total"] > rt["memory_mb_available"] > 0
    assert 0 < rt["memory_pct_used"] < 100 and rt["uptime_seconds"] > 0
    assert "load_avg" not in rt, "no load average on Windows; not faked"


def test_off_windows_everything_declines_quietly(monkeypatch):
    monkeypatch.setattr(winproc, "IS_WINDOWS", False)
    assert winproc.listening_pids(80) is None and winproc.port_is_listening(80) is None
    assert winproc.process_stats(os.getpid()) == {"memory_mb": None, "uptime_seconds": None, "cpu_ticks": None,
                                                 "pids": []}
    assert winproc.system_runtime() == {}
    assert lifecycle._nothing_listens_here("http://127.0.0.1:1/health") is False


def test_the_probe_only_short_cuts_for_this_box(monkeypatch):
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    monkeypatch.setattr(winproc, "port_is_listening", lambda port: False)
    assert lifecycle._nothing_listens_here("http://127.0.0.1:7250/health") is True
    assert lifecycle._nothing_listens_here("http://localhost:7250/") is True
    assert lifecycle._nothing_listens_here("http://192.0.2.9:7250/health") is False, "another box: probe it"
    assert lifecycle._nothing_listens_here("http://127.0.0.1/health") is False, "no port: nothing to look up"


@pytest.fixture()
def client(fake_home):
    import json
    from fastapi.testclient import TestClient
    from seren_observatory.app import create_app
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    with TestClient(create_app()) as c:
        c.headers["Authorization"] = "Bearer obs-secret"
        yield c


def test_a_box_with_no_shutdown_command_answers_instead_of_500(client, monkeypatch):
    def gone(*a, **k):
        raise FileNotFoundError(2, "No such file", a[0][0])
    monkeypatch.setattr(subprocess, "run", gone)
    r = client.post("/api/v1/system/reboot", json={"delay_minutes": 1})
    assert r.status_code == 200 and r.json()["scheduled"] is False
    assert "not on this box's PATH" in r.json()["error"]
    r = client.post("/api/v1/system/reboot/cancel")
    assert r.status_code == 200 and r.json()["cancelled"] is False


def test_on_windows_the_reboot_is_shutdown_exe_in_seconds(client, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    seen = []

    def run(argv, **k):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    r = client.post("/api/v1/system/reboot", json={"delay_minutes": 2}).json()
    assert r["scheduled"] is True and r["method"] == "shutdown /r /t 120"
    assert seen[-1][:4] == ["shutdown", "/r", "/t", "120"]
    r = client.post("/api/v1/system/reboot/cancel").json()
    assert r["cancelled"] is True and seen[-1] == ["shutdown", "/a"]


def test_a_windows_node_reports_memory_and_uptime(client, monkeypatch, node_manifest):
    monkeypatch.setattr(winproc, "system_runtime", lambda: {"memory_mb_total": 64000, "memory_mb_available": 32000,
                                                            "memory_pct_used": 50.0, "uptime_seconds": 1234})
    rt = client.get("/api/v1/system/node").json()["runtime"]
    if os.name == "nt":
        assert rt == {"memory_mb_total": 64000, "memory_mb_available": 32000, "memory_pct_used": 50.0,
                      "uptime_seconds": 1234}
    else:
        assert "memory_mb_total" in rt, "/proc answered; winproc is not asked"
