"""
What a service owns (20 Oct 2026). The ask: "probe should own its sub processes
(the docker containers whatever) ... same with workbench, since it can own sub
processes of its own (searxng whatever, this way we dont lie about whats
there and what it has)".

Pinned here:
- `parent` on a manifest rides on the status answer, so Lodestar folds the
  service under its owner
- `children: {docker_project: <glob>}` on a manifest makes the status carry
  every container whose compose project label matches, with the memory and
  cpu the shared docker snapshot already has; a stopped container is there
  too, as not running
- `docker ps --format json` labels are kept on the snapshot (they were
  dropped before), parsed from compose's "k=v,k=v"
- a manifest with neither key answers exactly as before
"""
from __future__ import annotations

import json
import time

import pytest

from seren_observatory import lifecycle, manifests


def test_the_manifest_helpers_read_the_two_keys():
    assert manifests.parent_of({"service": "searxng", "parent": "seren-workbench-wren"}) == "seren-workbench-wren"
    assert manifests.parent_of({"service": "searxng", "parent": "  "}) is None
    assert manifests.parent_of({"service": "searxng"}) is None
    assert manifests.children_spec({"children": {"docker_project": "seren-probe-*"}}) == {"docker_project": "seren-probe-*"}
    assert manifests.children_spec({"children": {}}) is None
    assert manifests.children_spec({"children": "seren-probe-*"}) is None


def test_docker_ps_keeps_the_labels(monkeypatch):
    rows = [
        {"ID": "abc123def456789", "Names": "seren-probe-target-memory-1", "State": "running", "Status": "Up 3 minutes",
         "Image": "seren-memory:5.2", "CreatedAt": "x",
         "Labels": "com.docker.compose.project=seren-probe-target,com.docker.compose.service=memory"},
        {"ID": "0000", "Names": "unrelated", "State": "exited", "Labels": ""},
    ]
    monkeypatch.setattr(lifecycle, "_docker_run",
                        lambda argv, timeout: {"ok": True, "stdout": "\n".join(json.dumps(r) for r in rows), "stderr": ""})
    states = lifecycle._docker_ps_all()
    assert states["seren-probe-target-memory-1"]["Labels"] == {
        "com.docker.compose.project": "seren-probe-target", "com.docker.compose.service": "memory"}
    assert states["unrelated"]["Labels"] == {}


@pytest.fixture
def snapshot(monkeypatch):
    """A warm docker cache: two of Probe's pod containers (one stopped) and
    searxng, which belongs to nobody's project."""
    monkeypatch.setitem(lifecycle._docker_cache, "states", {
        "seren-probe-target-memory-1": {"ID": "abc123def456789", "Name": "seren-probe-target-memory-1", "State": "running",
                                        "Status": "Up 3 minutes", "Image": "seren-memory:5.2",
                                        "Labels": {"com.docker.compose.project": "seren-probe-target"}},
        "seren-probe-target-loci-1": {"ID": "fff000111222333", "Name": "seren-probe-target-loci-1", "State": "exited",
                                      "Status": "Exited (0) 2 minutes ago", "Image": "seren-loci:5.2",
                                      "Labels": {"com.docker.compose.project": "seren-probe-target"}},
        "seren-searxng": {"ID": "999", "Name": "seren-searxng", "State": "running", "Status": "Up 2 days",
                          "Labels": {"com.docker.compose.project": "searxng"}},
    })
    monkeypatch.setitem(lifecycle._docker_cache, "stats", {"abc123def456": {"memory_mb": 210, "cpu_percent": 1.5},
                                                           "999": {"memory_mb": 90, "cpu_percent": 0.2}})
    monkeypatch.setitem(lifecycle._docker_cache, "states_at", time.monotonic())
    monkeypatch.setitem(lifecycle._docker_cache, "stats_at", time.monotonic())


def test_children_are_the_containers_of_the_matching_project(snapshot):
    kids = lifecycle.docker_children({"docker_project": "seren-probe-*"})
    assert [k["name"] for k in kids] == ["seren-probe-target-loci-1", "seren-probe-target-memory-1"]
    mem = {k["name"]: k for k in kids}["seren-probe-target-memory-1"]
    assert mem["running"] is True and mem["memory_mb"] == 210 and mem["cpu_percent"] == 1.5 and mem["kind"] == "container"
    loci = {k["name"]: k for k in kids}["seren-probe-target-loci-1"]
    assert loci["running"] is False and loci["memory_mb"] is None, "a stopped container is listed, as not running"
    assert lifecycle.docker_children({"docker_project": "nothing-*"}) == []
    assert lifecycle.docker_children({}) == []


async def test_status_carries_parent_and_children(snapshot, monkeypatch):
    async def by_type(manifest):
        return {"service": manifest["service"], "service_type": "systemd", "running": True}
    monkeypatch.setattr(lifecycle, "_status_by_type", by_type)

    probe = await lifecycle.status({"service": "seren-probe-wren", "service_type": "systemd", "port": 7430,
                                    "children": {"docker_project": "seren-probe-*"}})
    assert [k["name"] for k in probe["children"]] == ["seren-probe-target-loci-1", "seren-probe-target-memory-1"]
    assert "parent" not in probe

    searx = await lifecycle.status({"service": "searxng", "service_type": "docker_compose", "port": 8888,
                                    "parent": "seren-workbench-wren"})
    assert searx["parent"] == "seren-workbench-wren" and "children" not in searx

    plain = await lifecycle.status({"service": "llama", "service_type": "pid_file", "port": 8090})
    assert "parent" not in plain and "children" not in plain


async def test_a_broken_children_lookup_does_not_take_the_status_down(monkeypatch):
    async def by_type(manifest):
        return {"service": manifest["service"], "service_type": "systemd", "running": True}
    monkeypatch.setattr(lifecycle, "_status_by_type", by_type)

    async def boom(spec):
        raise RuntimeError("docker is having a day")
    monkeypatch.setattr(lifecycle, "children_of", boom)
    st = await lifecycle.status({"service": "seren-probe-wren", "service_type": "systemd", "port": 7430,
                                 "children": {"docker_project": "seren-probe-*"}})
    assert st["running"] is True and st["children"] == [] and "docker is having a day" in st["children_error"]


async def test_a_stale_docker_snapshot_is_served_at_once_and_freshened_behind(monkeypatch):
    """Lodestar gives a node 2s. docker stats takes 1.5s. A status answer must
    not carry it (20 Oct 2026: the nuc read offline for owning Probe's pods)."""
    import asyncio
    monkeypatch.setitem(lifecycle._docker_cache, "states", {
        "old-1": {"ID": "aaa", "Name": "old-1", "State": "running", "Labels": {"com.docker.compose.project": "seren-probe-x"}}})
    monkeypatch.setitem(lifecycle._docker_cache, "stats", {})
    monkeypatch.setitem(lifecycle._docker_cache, "states_at", time.monotonic() - 60)   # warm but stale
    monkeypatch.setitem(lifecycle._docker_cache, "stats_at", time.monotonic() - 60)
    monkeypatch.setitem(lifecycle._docker_cache, "refreshing", False)
    monkeypatch.setitem(lifecycle._docker_cache, "lock", None)

    def slow_ps():
        time.sleep(0.3)
        return {"new-1": {"ID": "bbb", "Name": "new-1", "State": "running",
                          "Labels": {"com.docker.compose.project": "seren-probe-x"}}}
    monkeypatch.setattr(lifecycle, "_docker_ps_all", slow_ps)
    monkeypatch.setattr(lifecycle, "_docker_stats_all", lambda: {"bbb": {"memory_mb": 7, "cpu_percent": 0.1}})

    t0 = time.monotonic()
    kids = await lifecycle.children_of({"docker_project": "seren-probe-*"})
    assert time.monotonic() - t0 < 0.2, "the stale snapshot is served without waiting for docker"
    assert [k["name"] for k in kids] == ["old-1"]
    for _ in range(100):                                 # the background refresh lands (states, then stats)
        await asyncio.sleep(0.02)
        if lifecycle._docker_cache["states"].get("new-1") and not lifecycle._docker_cache["refreshing"]:
            break
    assert [k["name"] for k in lifecycle.docker_children({"docker_project": "seren-probe-*"})] == ["new-1"]
    assert lifecycle._docker_cache["refreshing"] is False


async def test_a_cold_docker_snapshot_is_filled_inline_once(monkeypatch):
    monkeypatch.setitem(lifecycle._docker_cache, "states", {})
    monkeypatch.setitem(lifecycle._docker_cache, "stats", {})
    monkeypatch.setitem(lifecycle._docker_cache, "states_at", 0.0)
    monkeypatch.setitem(lifecycle._docker_cache, "stats_at", 0.0)
    monkeypatch.setitem(lifecycle._docker_cache, "refreshing", False)
    monkeypatch.setitem(lifecycle._docker_cache, "lock", None)
    monkeypatch.setattr(lifecycle, "_docker_ps_all", lambda: {"c": {"ID": "c", "Name": "c", "State": "running",
                                                                     "Labels": {"com.docker.compose.project": "seren-probe-x"}}})
    monkeypatch.setattr(lifecycle, "_docker_stats_all", lambda: {})
    kids = await lifecycle.children_of({"docker_project": "seren-probe-*"})
    assert [k["name"] for k in kids] == ["c"], "nothing to serve instead, so the first fill waits"
