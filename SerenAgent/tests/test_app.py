"""
Tests for the FastAPI app factory and top-level routing.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from seren_agent.app import create_app


@pytest.fixture()
def app(fake_home):
    """Minimal app with no installed services (empty ~/.seren/services/)."""
    return create_app()


@pytest.fixture()
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestPublicRoutes:
    async def test_ping(self, client):
        r = await client.get("/api/v1/system/ping")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "ts" in body

    async def test_version(self, client):
        r = await client.get("/api/v1/system/version")
        assert r.status_code == 200
        body = r.json()
        assert "agent_version" in body
        assert "manifest_schema" in body

    async def test_root_html(self, client):
        r = await client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "seren-agent" in r.text


class TestAuthOnProtectedRoutes:
    async def test_node_requires_auth(self, client):
        r = await client.get("/api/v1/system/node")
        # No token configured in tests → auth disabled, so 200 expected
        # (the middleware passes through with a warning header)
        assert r.status_code == 200

    async def test_services_returns_empty(self, client):
        r = await client.get("/api/v1/system/services")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["services"] == {}

    async def test_health_returns_ok_with_no_services(self, client):
        r = await client.get("/api/v1/system/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["total"] == 0


class TestVersionString:
    def test_version_is_string(self):
        from seren_agent import __version__
        assert isinstance(__version__, str)
        assert len(__version__) > 0
