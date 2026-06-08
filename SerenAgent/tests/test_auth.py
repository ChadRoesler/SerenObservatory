"""
Tests for seren_agent.auth - token loading and middleware behaviour.
"""
from __future__ import annotations

import json
import stat

import pytest

from seren_agent.auth import _constant_time_eq, load_token


class TestLoadToken:
    def test_returns_none_when_file_missing(self, fake_home):
        assert load_token() is None

    def test_loads_valid_token(self, fake_home, monkeypatch):
        secrets = fake_home / ".seren" / "secrets.json"
        secrets.write_text(json.dumps({"agent_token": "supersecret"}))
        import seren_agent.auth as auth_mod
        monkeypatch.setattr(auth_mod, "SECRETS_PATH", secrets)
        assert load_token() == "supersecret"

    def test_returns_none_on_bad_json(self, fake_home, monkeypatch):
        secrets = fake_home / ".seren" / "secrets.json"
        secrets.write_text("not json")
        import seren_agent.auth as auth_mod
        monkeypatch.setattr(auth_mod, "SECRETS_PATH", secrets)
        assert load_token() is None

    def test_returns_none_on_missing_key(self, fake_home, monkeypatch):
        secrets = fake_home / ".seren" / "secrets.json"
        secrets.write_text(json.dumps({"other_key": "value"}))
        import seren_agent.auth as auth_mod
        monkeypatch.setattr(auth_mod, "SECRETS_PATH", secrets)
        assert load_token() is None

    def test_returns_none_on_empty_token(self, fake_home, monkeypatch):
        secrets = fake_home / ".seren" / "secrets.json"
        secrets.write_text(json.dumps({"agent_token": ""}))
        import seren_agent.auth as auth_mod
        monkeypatch.setattr(auth_mod, "SECRETS_PATH", secrets)
        assert load_token() is None


class TestConstantTimeEq:
    def test_equal_strings(self):
        assert _constant_time_eq("hello", "hello") is True

    def test_unequal_strings_same_length(self):
        assert _constant_time_eq("hello", "world") is False

    def test_unequal_strings_different_length(self):
        assert _constant_time_eq("short", "muchlonger") is False

    def test_empty_strings_equal(self):
        assert _constant_time_eq("", "") is True

    def test_empty_vs_nonempty(self):
        assert _constant_time_eq("", "x") is False


class TestBearerMiddleware:
    """Integration-style tests using the Starlette TestClient (sync)."""

    @pytest.fixture()
    def app_no_token(self):
        """App with auth disabled (no token configured)."""
        from fastapi import FastAPI
        from seren_agent.auth import BearerAuthMiddleware

        app = FastAPI()
        app.add_middleware(BearerAuthMiddleware, expected_token=None)

        @app.get("/secret")
        async def secret():
            return {"data": "visible"}

        @app.post("/mutate")
        async def mutate():
            return {"data": "changed"}

        return app

    @pytest.fixture()
    def app_with_token(self):
        """App with a bearer token configured."""
        from fastapi import FastAPI
        from seren_agent.auth import BearerAuthMiddleware

        app = FastAPI()
        app.add_middleware(BearerAuthMiddleware, expected_token="mytoken")

        @app.get("/secret")
        async def secret():
            return {"data": "visible"}

        @app.get("/api/v1/system/ping")
        async def ping():
            return {"ok": True}

        return app

    def test_no_token_allows_all(self, app_no_token):
        from starlette.testclient import TestClient
        c = TestClient(app_no_token, raise_server_exceptions=True)
        r = c.get("/secret")
        assert r.status_code == 200
        assert r.headers.get("x-seren-auth") == "disabled-no-token-configured"

    def test_no_token_refuses_mutation(self, app_no_token):
        """With no token configured, state-changing methods must fail CLOSED
        (503) even though reads stay open - the unprovisioned-agent-on-0.0.0.0
        remote-reboot-button guard."""
        from starlette.testclient import TestClient
        c = TestClient(app_no_token, raise_server_exceptions=True)
        r = c.post("/mutate")
        assert r.status_code == 503

    def test_with_token_rejects_missing_auth(self, app_with_token):
        from starlette.testclient import TestClient
        c = TestClient(app_with_token, raise_server_exceptions=True)
        r = c.get("/secret")
        assert r.status_code == 401

    def test_with_token_rejects_wrong_token(self, app_with_token):
        from starlette.testclient import TestClient
        c = TestClient(app_with_token, raise_server_exceptions=True)
        r = c.get("/secret", headers={"Authorization": "Bearer wrongtoken"})
        assert r.status_code == 401

    def test_with_token_accepts_correct_token(self, app_with_token):
        from starlette.testclient import TestClient
        c = TestClient(app_with_token, raise_server_exceptions=True)
        r = c.get("/secret", headers={"Authorization": "Bearer mytoken"})
        assert r.status_code == 200

    def test_public_path_bypasses_auth(self, app_with_token):
        from starlette.testclient import TestClient
        c = TestClient(app_with_token, raise_server_exceptions=True)
        r = c.get("/api/v1/system/ping")
        assert r.status_code == 200