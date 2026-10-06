"""
POST /api/v1/service/{name}/ensure - the Observatory's link in the chain.

Design note: hippocampus => Lodestar => Observatory => start llama =>
the Observatory WAITS until llama is up => tells Lodestar. Pinned here:

- a service that already answers is ready at once and is not "started"
- a service that is down is started, and the answer waits for its health check
- one that never answers is stopped again and the answer says so
- a start that fails comes back as ok false with the script's words, not a 500
- it is a POST: the interlock applies; an unknown service is a 404
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory import lifecycle
from seren_observatory.app import create_app

OBS = {"Authorization": "Bearer obs-secret"}


@pytest.fixture()
def node(fake_home, monkeypatch):
    """A node with llama registered, and a model server we can script: it
    answers its health check `after` probes from being started."""
    (fake_home / ".seren" / "services" / "llama.json").write_text(json.dumps({
        "schema_version": 1, "service": "llama", "service_type": "pid_file", "port": 8090,
        "health_path": "/health", "start_script": "/x/start_llama.sh", "stop_script": "/x/stop_llama.sh"}))
    (fake_home / ".seren" / "services" / "coral.json").write_text(json.dumps({
        "schema_version": 1, "service": "coral", "service_type": "library"}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    box = {"running": False, "probes_until_up": 2, "probes": 0, "starts": 0, "stops": 0, "start_ok": True}

    async def probe(manifest):
        if not box["running"]:
            return {"ok": False, "error": "connection refused"}
        box["probes"] += 1
        return {"ok": box["probes"] > box["probes_until_up"]}

    async def start(manifest):
        box["starts"] += 1
        if not box["start_ok"]:
            return {"ok": False, "exit_code": 1, "stderr": "model.gguf: no such file"}
        box["running"], box["probes"] = True, 0
        return {"ok": True, "pid": 4242}

    async def stop(manifest):
        box["stops"] += 1
        box["running"] = False
        return {"ok": True}

    monkeypatch.setattr(lifecycle, "probe_port", probe)
    monkeypatch.setattr(lifecycle, "start", start)
    monkeypatch.setattr(lifecycle, "stop", stop)
    monkeypatch.setattr(lifecycle, "ENSURE_POLL_SECONDS", 0.01)
    return create_app(), box


async def _post(app, path, body=None, headers=OBS):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        return await c.post(path, json=body) if body is not None else await c.post(path)


async def test_a_down_service_is_started_and_the_answer_waits_for_it(node):
    app, box = node
    r = await _post(app, "/api/v1/service/llama/ensure", {"holder": "seren-hippocampus", "reason": "a sleep"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["ready"] and d["started"] and not d["already_running"], d
    assert (d["service"], d["port"], d["health_path"]) == ("llama", 8090, "/health")
    assert box["starts"] == 1 and box["probes"] == 3, "it kept asking until the model answered"


async def test_a_service_that_already_answers_is_ready_and_not_started(node):
    app, box = node
    box["running"], box["probes_until_up"] = True, 0
    d = (await _post(app, "/api/v1/service/llama/ensure")).json()          # no body: the defaults
    assert d["ok"] and d["ready"] and d["already_running"] and not d["started"], d
    assert box["starts"] == 0, "whoever started it still owns it"


async def test_one_that_never_answers_is_stopped_again_and_says_so(node):
    app, box = node
    box["probes_until_up"] = 10_000
    d = (await _post(app, "/api/v1/service/llama/ensure", {"wait_seconds": 0.1})).json()
    assert d["ok"] is False and d["ready"] is False, d
    assert "did not answer its health check within" in d["error"] and "stopped again" in d["error"]
    assert box["starts"] == 1 and box["stops"] == 1 and box["running"] is False


async def test_a_start_that_fails_is_an_answer_not_a_500(node):
    app, box = node
    box["start_ok"] = False
    r = await _post(app, "/api/v1/service/llama/ensure", {"holder": "h"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "llama did not start: model.gguf: no such file" == r.json()["error"]
    assert box["stops"] == 0


async def test_the_interlock_a_library_and_an_unknown_service(node):
    app, box = node
    assert (await _post(app, "/api/v1/service/llama/ensure", headers={})).status_code in (401, 403)
    assert box["starts"] == 0
    d = (await _post(app, "/api/v1/service/coral/ensure")).json()
    assert d["ok"] is False and "no port to wait on" in d["error"]
    assert (await _post(app, "/api/v1/service/whisper/ensure")).status_code == 404
