"""
A service's snapshots, reached through the Observatory (stores_routes).

The cluster head holds one token per node - the Observatory's - and the
Observatory presents each service's own bearer, read from the config its
manifest names. Pinned here:

- GET /stores and /stores/snapshots are relayed, with the service's bearer
- everything needs the Observatory's token once one is provisioned (the
  interlock); POST /stores/snapshot relays the reason
- the archive comes back as the bytes the service sent, with its headers
- a service with no store routes is a plain 404; one that refuses the bearer
  is a 502 that says so; one not installed is 404
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from seren_observatory.app import create_app
from seren_sinew.stores import Store, StoreKeeper, add_store_routes, unpack_snapshot
from seren_observatory import stores_routes

OBS = {"Authorization": "Bearer obs-secret"}


class _Bearer(BaseHTTPMiddleware):
    def __init__(self, app, token):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)


@pytest.fixture()
def service(tmp_path):
    """A stand-in Seren service: Sinew's store routes behind a bearer."""
    f = tmp_path / "voice.json"
    f.write_text("the card")
    keeper = StoreKeeper("seren-memory", lambda: [Store("memory", "file", str(f), "the one file")], tmp_path / "backups")
    app = Starlette()
    add_store_routes(app, lambda: keeper)
    app.add_middleware(_Bearer, token="svc-secret")
    return app, keeper


@pytest.fixture()
def wired(fake_home, service, monkeypatch):
    """The Observatory, with a manifest for the service and the service's
    config (holding its bearer), and HTTP to the service routed in-process."""
    app, keeper = service
    cfg = fake_home / "seren-memory.yaml"
    cfg.write_text("server:\n  bearer_token: svc-secret\n")
    (fake_home / ".seren" / "services" / "SerenMemory-wren.json").write_text(json.dumps({
        "schema_version": 2, "service": "SerenMemory-wren", "service_type": "windows_service",
        "windows_service": "SerenMemory-wren", "port": 7267, "health_url": "http://127.0.0.1:7267/health",
        "config_path": str(cfg)}))
    (fake_home / ".seren" / "services" / "SerenLoci-wren.json").write_text(json.dumps({
        "schema_version": 2, "service": "SerenLoci-wren", "service_type": "windows_service",
        "windows_service": "SerenLoci-wren", "port": 7266, "config_path": str(cfg)}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    calls = []

    class Client(AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = ASGITransport(app=app)
            kw.setdefault("base_url", "http://127.0.0.1:7267")
            super().__init__(*a, **kw)

        async def request(self, method, url, **kw):
            calls.append((method, str(url), dict(kw.get("headers") or {})))
            return await super().request(method, url, **kw)
    monkeypatch.setattr(stores_routes.httpx, "AsyncClient", Client)
    return create_app(), keeper, calls


async def test_reads_are_relayed_with_the_services_own_bearer(wired):
    app, keeper, calls = wired
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=OBS) as c:
        r = await c.get("/api/v1/service/SerenMemory-wren/stores")
        assert r.status_code == 200, r.text
        assert r.json()["stores"][0]["name"] == "memory" and r.json()["snapshots"]["count"] == 0
        assert calls[-1][1] == "http://127.0.0.1:7267/stores" and calls[-1][2]["Authorization"] == "Bearer svc-secret"
        assert (await c.get("/api/v1/service/SerenMemory-wren/stores/snapshots")).json()["count"] == 0


async def test_taking_a_snapshot_needs_the_interlock_and_relays_the_reason(wired):
    app, keeper, calls = wired
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=OBS) as c:
        bare = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        assert (await bare.post("/api/v1/service/SerenMemory-wren/stores/snapshot")).status_code in (401, 403)
        await bare.aclose()
        r = await c.post("/api/v1/service/SerenMemory-wren/stores/snapshot", json={"reason": "Lodestar asked"})
        assert r.status_code == 200, r.text
        assert r.json()["snapshot"]["reason"] == "Lodestar asked"
        assert keeper.list()[0]["reason"] == "Lodestar asked"


async def test_the_archive_comes_back_whole(wired, tmp_path):
    app, keeper, calls = wired
    sid = keeper.snapshot("nightly")["id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=OBS) as c:
        r = await c.get(f"/api/v1/service/SerenMemory-wren/stores/snapshots/{sid}/archive")
        assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
        assert r.headers["x-seren-snapshot"] == sid and r.headers["x-seren-service"] == "seren-memory"
        got = unpack_snapshot(r.content, tmp_path / "stash", expect_id=sid)
        assert (got / "raw" / "memory" / "voice.json").read_text() == "the card"
        assert (await c.get("/api/v1/service/SerenMemory-wren/stores/snapshots/nope/archive")).status_code == 404
        assert (await c.get("/api/v1/service/SerenMemory-wren/stores/snapshots/..%2Fx/archive")).status_code in (400, 404), "no path in a snapshot id"


async def test_plain_answers_for_the_three_ways_it_cannot(wired, fake_home, monkeypatch):
    app, keeper, calls = wired
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=OBS) as c:
        r = await c.get("/api/v1/service/SerenProbe-wren/stores")
        assert r.status_code == 404 and "not installed" in r.json()["detail"]
        # a service whose bearer cannot be read: the service says 401, the Observatory says why
        (fake_home / "seren-memory.yaml").write_text("server:\n  bearer_token: ''\n")
        r = await c.get("/api/v1/service/SerenMemory-wren/stores")
        assert r.status_code == 502 and "bearer could not be read" in r.json()["detail"]
    # a service with no store routes at all
    plain = Starlette()
    plain.add_middleware(_Bearer, token="svc-secret")
    (fake_home / "seren-memory.yaml").write_text("server:\n  bearer_token: svc-secret\n")

    class Client(AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = ASGITransport(app=plain)
            kw.setdefault("base_url", "http://127.0.0.1:7267")
            super().__init__(*a, **kw)
    monkeypatch.setattr(stores_routes.httpx, "AsyncClient", Client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=OBS) as c:
        r = await c.get("/api/v1/service/SerenMemory-wren/stores")
        assert r.status_code == 404 and "keeps no snapshots" in r.json()["detail"]


def test_where_a_service_listens_and_its_bearer(tmp_path):
    assert stores_routes.service_base_url({"health_url": "http://127.0.0.1:7267/health"}) == "http://127.0.0.1:7267"
    assert stores_routes.service_base_url({"port": 7266}) == "http://127.0.0.1:7266"
    assert stores_routes.service_base_url({"port": 0}) is None and stores_routes.service_base_url({}) is None
    cfg = tmp_path / "s.yaml"
    cfg.write_text("server:\n  bearer_token: abc\n")
    assert stores_routes.service_bearer({"config_path": str(cfg)}) == "abc"
    assert stores_routes.service_bearer({}) == "" and stores_routes.service_bearer({"config_path": str(tmp_path / "no.yaml")}) == ""
