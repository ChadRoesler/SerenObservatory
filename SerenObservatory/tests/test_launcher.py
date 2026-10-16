"""
launcher: a per-user app the Observatory starts detached and knows by the pid
file it writes (16 Oct 2026). The desktop front door is a tray app: no
Windows service can host it (a service has no desktop session), so until
this the desktop's Observatory listed nothing but itself and the one app
people use there was upgraded by hand or not at all.

The app here is a real child process: a script that writes its pid where the
manifest says and then sleeps. So these run the real Popen, the real
detaching flags, the real taskkill or SIGTERM - on either platform.

  - start waits for a LIVE pid in the file, says the pid, detaches the child;
  - a second start is already_running, not a second process;
  - status is running with the pid; after stop it is not, and the file is gone;
  - a stale pid file (a process that is gone) reads as not running, and start
    replaces it rather than believing it;
  - a command that fails before writing the file is an error with its exit code;
  - restart is a stop that finished, then a start - a new pid;
  - a manifest without start_command or pid_path says which is missing;
  - reclaim leaves a launcher alone, like every non-GPU service.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time

import pytest

from seren_observatory import lifecycle, manifests
from seren_observatory.system_routes import reclaim_plan

CHILD = textwrap.dedent("""
    import os, sys, time
    with open(sys.argv[1], "w") as f:
        f.write(str(os.getpid()))
    time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 120)
""")


@pytest.fixture
def desk(tmp_path):
    script = tmp_path / "app.py"
    script.write_text(CHILD, encoding="utf-8")
    pid_path = tmp_path / "app.pid"
    m = {"schema_version": 2, "service": "desk-app", "service_type": "launcher",
         "start_command": [sys.executable, str(script), str(pid_path)],
         "pid_path": str(pid_path), "cwd": str(tmp_path), "port": 0, "start_timeout_seconds": 20}
    yield m
    pid = lifecycle._launcher_pid(m)                  # never leave a sleeper behind
    if pid:
        lifecycle._terminate(pid)


def _gone(pid: int, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not lifecycle.pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_the_type_is_known():
    assert "launcher" in manifests.SERVICE_TYPES
    assert manifests.service_type({"service_type": "launcher", "port": 7271}) == "launcher"


def test_pid_alive_is_a_probe_not_a_kill():
    assert lifecycle.pid_alive(os.getpid()) is True
    done = subprocess.run([sys.executable, "-c", "pass"])
    assert done.returncode == 0
    assert lifecycle.pid_alive(0) is False and lifecycle.pid_alive(-1) is False
    assert lifecycle.pid_alive(os.getpid()) is True, "asking did not harm the asker"


def test_start_status_stop(desk):
    res = lifecycle._launcher_start(desk)
    assert res["ok"] and res["pid"] > 0 and lifecycle.pid_alive(res["pid"]), res
    assert res["pid"] != os.getpid()

    again = lifecycle._launcher_start(desk)
    assert again == {"ok": True, "already_running": True, "pid": res["pid"]}

    st = asyncio.run(lifecycle.status(desk))
    assert st["running"] is True and st["pid"] == res["pid"] and st["service_type"] == "launcher"
    assert st["port_health"] is None, "port 0: nothing to probe"
    assert st["orchestrated"] is False

    stopped = lifecycle._launcher_stop(desk)
    assert stopped["ok"] and stopped["was_running"] is True and stopped["pid"] == res["pid"]
    assert _gone(res["pid"])
    assert not os.path.exists(desk["pid_path"]), "the file goes with the process"
    assert asyncio.run(lifecycle.status(desk))["running"] is False
    assert lifecycle._launcher_stop(desk) == {"ok": True, "was_running": False}


def test_a_stale_pid_file_is_not_believed(desk, tmp_path):
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    with open(desk["pid_path"], "w") as f:
        f.write(str(done.pid))                        # a pid that was, and is not
    assert _gone(done.pid)
    assert lifecycle._launcher_pid(desk) is None
    assert asyncio.run(lifecycle.status(desk))["running"] is False
    res = lifecycle._launcher_start(desk)
    assert res["ok"] and res.get("already_running") is None, "started, rather than trusting the old file"
    assert res["pid"] != done.pid


def test_a_command_that_dies_first_is_an_error_with_its_exit_code(desk, tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    desk["start_command"] = [sys.executable, str(bad)]
    res = lifecycle._launcher_start(desk)
    assert res["ok"] is False and res["exit_code"] == 3 and "exited 3" in res["error"]


def test_a_command_that_never_writes_the_file_times_out(desk, tmp_path):
    quiet = tmp_path / "quiet.py"
    quiet.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    desk["start_command"] = [sys.executable, str(quiet)]
    desk["start_timeout_seconds"] = 1
    res = lifecycle._launcher_start(desk)
    assert res["ok"] is False and "within 1s" in res["error"] and res["spawned"] > 0
    lifecycle._terminate(res["spawned"])


def test_restart_is_a_stop_that_finished_then_a_start(desk):
    first = lifecycle._launcher_start(desk)["pid"]
    res = asyncio.run(lifecycle.restart(desk))
    assert res["ok"] and res["stop"]["was_running"] is True and res["start"]["pid"] != first
    assert _gone(first) and lifecycle.pid_alive(res["start"]["pid"])


def test_missing_fields_are_named():
    assert "start_command" in lifecycle._launcher_start({"service_type": "launcher", "pid_path": "x"})["error"]
    assert "pid_path" in lifecycle._launcher_start({"service_type": "launcher", "start_command": ["x"]})["error"]
    assert "start_command" in lifecycle._launcher_start({"service_type": "launcher", "start_command": "not a list",
                                                         "pid_path": "x"})["error"]
    st = asyncio.run(lifecycle.status({"service": "s", "service_type": "launcher"}))
    assert st["running"] is False and "pid_path" in st["error"]


def test_reclaim_leaves_a_launcher_alone():
    fleet = {"llama": {"service": "llama", "service_type": "pid_file", "port": 8090},
             "desk-app": {"service": "desk-app", "service_type": "launcher", "port": 7271,
                          "start_command": ["x"], "pid_path": "p"}}
    cands, kept = reclaim_plan(fleet, exclude=set(), include=set(), everything=False)
    assert [n for n, _ in cands] == ["llama"]
    assert any(k["service"] == "desk-app" for k in kept)
