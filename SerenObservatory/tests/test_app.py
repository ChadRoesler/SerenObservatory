"""
Tests for the FastAPI app factory and top-level routing.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory.app import create_app


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
        assert "observatory_version" in body
        assert "manifest_schema" in body

    async def test_root_html(self, client):
        r = await client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "seren-observatory" in r.text
        # dark, in the glance's palette, and it points at the glance
        assert 'name="color-scheme" content="dark"' in r.text and "--bg: #14141f" in r.text
        assert 'href="/viewer"' in r.text


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


class TestSecretsPathWiring:
    """create_app reads the token from the CONFIGURED secrets file and tells
    the operator that same path when it is missing - per-install roots
    (~/seren/<install>/secrets.json) would be useless otherwise."""

    async def _post_mutation(self, app):
        # A service that doesn't exist, NOT /system/reboot: if the interlock
        # ever regressed this should 404, not schedule a real reboot.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return (await c.post("/api/v1/service/no-such-service/restart"),
                    await c.get("/"))

    async def test_configured_path_arms_auth(self, fake_home):
        import json
        from seren_observatory.config import ObservatoryConfig
        where = fake_home / "seren" / "alpha" / "secrets.json"
        where.parent.mkdir(parents=True)
        where.write_text(json.dumps({"observatory_token": "alpha-tok"}))
        app = create_app(ObservatoryConfig(secrets_path="~/seren/alpha/secrets.json"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/api/v1/system/services")).status_code == 401
            r = await c.get("/api/v1/system/services",
                            headers={"Authorization": "Bearer alpha-tok"})
            assert r.status_code == 200

    async def test_missing_file_names_the_configured_path(self, fake_home):
        from seren_observatory.config import ObservatoryConfig
        where = fake_home / "seren" / "alpha" / "secrets.json"
        app = create_app(ObservatoryConfig(secrets_path=str(where)))
        r, root = await self._post_mutation(app)
        assert r.status_code == 503
        assert str(where) in r.json()["detail"]
        assert str(where) in root.text

    async def test_env_beats_configured_path(self, fake_home, monkeypatch):
        from seren_observatory.config import ObservatoryConfig
        via_env = fake_home / "seren" / "beta" / "secrets.json"
        monkeypatch.setenv("SEREN_OBSERVATORY_SECRETS", str(via_env))
        app = create_app(ObservatoryConfig(secrets_path="~/seren/alpha/secrets.json"))
        r, _ = await self._post_mutation(app)
        assert r.status_code == 503
        assert str(via_env) in r.json()["detail"]


class TestManifestsDirWiring:
    """create_app points every roster read at the CONFIGURED manifests_dir,
    so two installs on one host each see only their own services."""

    @staticmethod
    def _roster(fake_home, install, *names):
        import json
        d = fake_home / "seren" / install / "manifests"
        d.mkdir(parents=True)
        for n in names:
            (d / f"{n}.json").write_text(json.dumps(
                {"schema_version": 2, "service": n, "service_type": "library", "port": 0}))
        return d

    async def _get(self, app, path):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.get(path)

    async def test_configured_dir_is_the_roster(self, fake_home, pid_service_manifest):
        from seren_observatory.config import ObservatoryConfig
        self._roster(fake_home, "alpha", "coral")
        app = create_app(ObservatoryConfig(manifests_dir="~/seren/alpha/manifests"))
        body = (await self._get(app, "/api/v1/system/services")).json()
        # alpha's coral from the install roster, and the box's llama from
        # ~/.seren/services - each saying where it came from.
        assert sorted(body["services"]) == ["coral", "llama"]
        assert body["services"]["coral"]["manifest"]["_roster"] == "install"
        assert body["services"]["llama"]["manifest"]["_roster"] == "box"
        assert (await self._get(app, "/api/v1/service/coral/manifest")).status_code == 200

    async def test_env_beats_configured_dir(self, fake_home, monkeypatch):
        from seren_observatory.config import ObservatoryConfig
        self._roster(fake_home, "alpha", "coral")
        beta = self._roster(fake_home, "beta", "kokoro")
        monkeypatch.setenv("SEREN_OBSERVATORY_MANIFESTS", str(beta))
        app = create_app(ObservatoryConfig(manifests_dir="~/seren/alpha/manifests"))
        body = (await self._get(app, "/api/v1/system/services")).json()
        assert list(body["services"]) == ["kokoro"]

    async def test_root_page_names_the_roster(self, fake_home):
        from seren_observatory.config import ObservatoryConfig
        where = self._roster(fake_home, "alpha")
        app = create_app(ObservatoryConfig(manifests_dir=str(where)))
        root = await self._get(app, "/")
        assert str(where) in root.text
        assert str(fake_home / ".seren" / "services") in root.text
        assert str(fake_home / ".seren" / "node.json") in root.text


class TestVersionString:
    def test_version_is_string(self):
        from seren_observatory import __version__
        assert isinstance(__version__, str)
        assert len(__version__) > 0
