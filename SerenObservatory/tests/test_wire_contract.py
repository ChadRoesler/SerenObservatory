"""
The wire contract: every answer this Observatory gives Lodestar parses
STRICTLY with the shapes both sides share (seren_sinew.cluster.dtos).

Why this exists. The head and the node used to spell the wire separately,
and they drifted: /system/version was renamed from `agent_version` to
`observatory_version` and the head's version call returned None on every
node without a word. Now the dataclasses live in Sinew, this test parses
each route's real answer with them strictly, and a key this service adds
that the shape does not know fails HERE, on the side that changed, instead
of being dropped on the floor by the head.

What is covered: the system routes (ping, version, node, thermal, services,
health, reclaim) and the universal service verbs (manifest, status, health,
start, stop, restart, logs) over a pid_file service that is not running and
a library-mode one. The reboot routes shell out to sudo and are the node's
business; the Observatory update route takes a tarball. Neither is on the
head's polling path, so neither is here.

Skips when the installed Sinew predates `seren_sinew.cluster` (CI installs
the release from PyPI until 5.4.0 is out).
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

cluster = pytest.importorskip("seren_sinew.cluster", reason="the installed seren-sinew predates seren_sinew.cluster")

from seren_sinew.cluster import (  # noqa: E402
    HealthResponse,
    LogsResponse,
    NodeResponse,
    PingResponse,
    PortHealth,
    ReclaimResponse,
    ServiceLifecycleResponse,
    ServiceManifest,
    ServiceStatus,
    ServicesResponse,
    ThermalResponse,
    VersionResponse,
    drift,
    from_dict,
)

from seren_observatory.app import create_app  # noqa: E402

OBS = {"Authorization": "Bearer obs-secret"}


@pytest.fixture()
def roster(fake_home, pid_service_manifest, library_service_manifest, node_manifest):
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    return fake_home


@pytest.fixture()
async def client(roster):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c


def _holds(data, shape):
    """The answer parses strictly, and drift reports nothing: one assertion
    message that names the route's keys the shape does not know."""
    problems = drift(data, shape)
    assert not problems, "the wire moved: " + "; ".join(str(p) for p in problems) + f"\nanswer: {data}"
    return from_dict(data, shape, strict=True)


async def test_the_system_routes_answer_in_the_shared_shapes(client):
    ping = _holds((await client.get("/api/v1/system/ping")).json(), PingResponse)
    assert ping.ok and ping.ts > 0

    version = _holds((await client.get("/api/v1/system/version")).json(), VersionResponse)
    assert version.observatory_version and version.agent_version == version.observatory_version
    assert version.manifest_schema >= 1 and isinstance(version.updates, dict)

    node = _holds((await client.get("/api/v1/system/node", headers=OBS)).json(), NodeResponse)
    assert node.manifest.hostname == "test-jetson"
    assert node.manifest.extra.get("hardware") == "Jetson AGX Orin", "what node.json adds rides in extra"
    assert isinstance(node.carabiners, list)

    thermal = _holds((await client.get("/api/v1/system/thermal", headers=OBS)).json(), ThermalResponse)
    assert isinstance(thermal.available, bool) and isinstance(thermal.zones, list)

    services = _holds((await client.get("/api/v1/system/services", headers=OBS)).json(), ServicesResponse)
    assert services.count == 2 and set(services.services) == {"llama", "coral"}
    assert services.services["llama"].status.running is False
    assert services.services["coral"].status.library_mode is True

    health = _holds((await client.get("/api/v1/system/health", headers=OBS)).json(), HealthResponse)
    assert health.total == 2 and health.not_running == ["llama"] and health.idle == []

    reclaim = _holds((await client.post("/api/v1/system/reclaim", json={}, headers=OBS)).json(), ReclaimResponse)
    assert reclaim.stopped == [] and reclaim.failed == []
    assert {k["service"] for k in reclaim.kept} == {"llama", "coral"}, "kept rows carry a why"


async def test_the_universal_service_verbs_answer_in_the_shared_shapes(client):
    manifest = _holds((await client.get("/api/v1/service/llama/manifest", headers=OBS)).json(), ServiceManifest)
    assert manifest.service == "llama" and manifest.port == 8080 and manifest.service_type == "pid_file"

    status = _holds((await client.get("/api/v1/service/llama/status", headers=OBS)).json(), ServiceStatus)
    assert status.running is False and status.service_type == "pid_file"
    lib = _holds((await client.get("/api/v1/service/coral/status", headers=OBS)).json(), ServiceStatus)
    assert lib.library_mode is True and lib.running is True

    health = _holds((await client.get("/api/v1/service/llama/health", headers=OBS)).json(), PortHealth)
    assert health.ok is False and health.error is not None, "nothing listens on 8080: ok false, error named"
    lib_health = _holds((await client.get("/api/v1/service/coral/health", headers=OBS)).json(), PortHealth)
    assert lib_health.ok is True and lib_health.library_mode is True and lib_health.service == "coral"

    start = _holds((await client.post("/api/v1/service/llama/start", headers=OBS)).json(), ServiceLifecycleResponse)
    assert start.ok is False and "start script not found" in (start.error or "")
    stop = _holds((await client.post("/api/v1/service/llama/stop", headers=OBS)).json(), ServiceLifecycleResponse)
    assert stop.ok is True and stop.was_running is False
    restart = _holds((await client.post("/api/v1/service/llama/restart", headers=OBS)).json(),
                     ServiceLifecycleResponse)
    assert restart.ok is False and isinstance(restart.stop, dict) and isinstance(restart.start, dict)
    assert restart.port_released is True, "a restart's two halves ride along, and the port dance"
    refused = _holds((await client.post("/api/v1/service/coral/start", headers=OBS)).json(),
                     ServiceLifecycleResponse)
    assert refused.ok is False and refused.service_type == "library"

    logs = _holds((await client.get("/api/v1/service/llama/logs?lines=5", headers=OBS)).json(), LogsResponse)
    assert logs.ok is True and logs.lines == [] and logs.note, "no log yet is an answer, not an error"


async def test_a_key_the_shape_does_not_know_is_caught_not_dropped():
    """The guard the two tests above lean on: a fixed shape refuses drift."""
    answer = {"ok": True, "total": 0, "healthy": 0, "idle": [], "degraded": [], "not_running": [], "mood": "fine"}
    problems = drift(answer, HealthResponse)
    assert [p.unknown for p in problems] == [["mood"]]
    with pytest.raises(cluster.WireDrift):
        from_dict(answer, HealthResponse, strict=True)
