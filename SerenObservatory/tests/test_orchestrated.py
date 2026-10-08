"""
`orchestrated`: a service Lodestar starts on demand is idle when off, not down.

A service can be flagged 'orchestrated' so it does not report as unhealthy
when it is healthy and simply not on because of orchestration. Pinned here:

- the flag rides on every status answer; off + orchestrated = idle
- /system/health counts an idle service as idle, not not_running, and stays ok
- POST /service/{name}/orchestrated sets and clears it (the interlock applies)
- an ensure that STARTED a service marks it orchestrated by itself
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory import lifecycle, manifests
from seren_observatory.app import create_app

OBS = {"Authorization": "Bearer obs-secret"}


@pytest.fixture()
def node(fake_home, monkeypatch):
    svc = fake_home / ".seren" / "services"
    (svc / "llama.json").write_text(json.dumps({
        "schema_version": 1, "service": "llama", "service_type": "pid_file", "port": 8090, "orchestrated": True,
        "health_path": "/health", "start_script": "/x/start.sh", "stop_script": "/x/stop.sh"}))
    (svc / "kokoro.json").write_text(json.dumps({
        "schema_version": 1, "service": "kokoro", "service_type": "pid_file", "port": 8880,
        "health_path": "/health", "start_script": "/x/start_k.sh", "stop_script": "/x/stop_k.sh"}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    box = {"running": set()}

    async def status(manifest):
        name = manifest.get("service")
        return {"service": name, "service_type": "pid_file", "running": name in box["running"],
                **({"port_health": {"ok": True}} if name in box["running"] else {})}
    monkeypatch.setattr(lifecycle, "_status_by_type", status)
    return box


async def test_off_and_orchestrated_is_idle_not_unhealthy(node):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        st = (await c.get("/api/v1/service/llama/status", headers=OBS)).json()
        assert st["running"] is False and st["orchestrated"] is True and st["idle"] is True
        st2 = (await c.get("/api/v1/service/kokoro/status", headers=OBS)).json()
        assert st2["orchestrated"] is False and "idle" not in st2
        h = (await c.get("/api/v1/system/health", headers=OBS)).json()
        assert h["idle"] == ["llama"] and h["not_running"] == ["kokoro"] and h["ok"] is False
        # kokoro comes up: all is well, llama still resting
        node["running"].add("kokoro")
        h = (await c.get("/api/v1/system/health", headers=OBS)).json()
        assert h["ok"] is True and h["idle"] == ["llama"] and h["not_running"] == [] and h["healthy"] == 1
        # llama in use: running, counted healthy, no longer idle
        node["running"].add("llama")
        h = (await c.get("/api/v1/system/health", headers=OBS)).json()
        assert h["ok"] is True and h["idle"] == [] and h["healthy"] == 2
        listed = (await c.get("/api/v1/system/services", headers=OBS)).json()["services"]
        assert listed["llama"]["status"]["orchestrated"] is True


async def test_the_flag_is_set_and_cleared_by_route_and_written_to_the_manifest(node, fake_home):
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/service/kokoro/orchestrated", json={"orchestrated": True}, headers=OBS)
        assert r.status_code == 200 and r.json()["orchestrated"] is True
        on_disk = json.loads((fake_home / ".seren" / "services" / "kokoro.json").read_text())
        assert on_disk["orchestrated"] is True and on_disk["port"] == 8880, "the one key written, the rest kept"
        assert "_roster" not in on_disk
        h = (await c.get("/api/v1/system/health", headers=OBS)).json()
        assert h["idle"] == ["kokoro", "llama"] and h["ok"] is True
        r = await c.post("/api/v1/service/kokoro/orchestrated", json={"orchestrated": False}, headers=OBS)
        assert r.json()["orchestrated"] is False
        assert manifests.is_orchestrated(manifests.load_service("kokoro")) is False
        assert (await c.post("/api/v1/service/nope/orchestrated", json={}, headers=OBS)).status_code == 404
        assert (await c.post("/api/v1/service/kokoro/orchestrated", json={})).status_code in (401, 403), "the interlock"


async def test_an_ensure_that_started_a_service_marks_it_orchestrated(node, fake_home, monkeypatch):
    starts = []

    async def start(manifest):
        starts.append(manifest["service"])
        node["running"].add(manifest["service"])
        return {"ok": True}

    async def probe(manifest):
        return {"ok": manifest["service"] in node["running"]}
    monkeypatch.setattr(lifecycle, "start", start)
    monkeypatch.setattr(lifecycle, "probe_port", probe)
    monkeypatch.setattr(lifecycle, "ENSURE_POLL_SECONDS", 0.01)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert manifests.is_orchestrated(manifests.load_service("kokoro")) is False
        r = await c.post("/api/v1/service/kokoro/ensure", json={"holder": "seren-hippocampus", "wait_seconds": 5}, headers=OBS)
        body = r.json()
        assert body["ok"] and body["ready"] and body["started"], body
        assert manifests.is_orchestrated(manifests.load_service("kokoro")) is True, "someone asked, we started it: orchestrated"
        # llama was already flagged; an ensure of a running service changes nothing
        node["running"].add("llama")
        r = await c.post("/api/v1/service/llama/ensure", json={"holder": "x"}, headers=OBS)
        assert r.json()["already_running"] is True and starts == ["kokoro"]
