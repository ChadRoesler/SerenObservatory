"""
Tests for seren_observatory.auth - token loading and middleware behaviour.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from seren_observatory.auth import (
    SECRETS_ENV, _constant_time_eq, load_token, resolve_secrets_path,
)


class TestLoadToken:
    def test_returns_none_when_file_missing(self, fake_home):
        assert load_token() is None

    def test_loads_valid_token(self, fake_home, monkeypatch):
        secrets = fake_home / ".seren" / "secrets.json"
        secrets.write_text(json.dumps({"observatory_token": "supersecret"}))
        assert load_token(secrets) == "supersecret"

    def test_returns_none_on_bad_json(self, fake_home, monkeypatch):
        secrets = fake_home / ".seren" / "secrets.json"
        secrets.write_text("not json")
        assert load_token(secrets) is None

    def test_returns_none_on_missing_key(self, fake_home, monkeypatch):
        secrets = fake_home / ".seren" / "secrets.json"
        secrets.write_text(json.dumps({"other_key": "value"}))
        assert load_token(secrets) is None

    def test_returns_none_on_empty_token(self, fake_home, monkeypatch):
        secrets = fake_home / ".seren" / "secrets.json"
        secrets.write_text(json.dumps({"observatory_token": ""}))
        assert load_token(secrets) is None


class TestSecretsPath:
    """Where the token file is: $SEREN_OBSERVATORY_SECRETS -> the yaml's
    server.secrets_path -> ~/.seren/secrets.json. Resolved at call time so
    per-install roots (~/seren/<install>/...) actually take effect."""

    def test_default_is_home_dot_seren(self, fake_home):
        assert resolve_secrets_path() == fake_home / ".seren" / "secrets.json"
        assert resolve_secrets_path() == Path(os.path.expanduser("~/.seren/secrets.json"))

    def test_configured_path_is_used_and_expanded(self, fake_home):
        got = resolve_secrets_path("~/seren/alpha/secrets.json")
        assert got == fake_home / "seren" / "alpha" / "secrets.json"

    def test_env_beats_configured(self, fake_home, monkeypatch):
        monkeypatch.setenv(SECRETS_ENV, "~/seren/beta/secrets.json")
        got = resolve_secrets_path("~/seren/alpha/secrets.json")
        assert got == fake_home / "seren" / "beta" / "secrets.json"

    def test_empty_env_counts_as_unset(self, fake_home, monkeypatch):
        monkeypatch.setenv(SECRETS_ENV, "")
        got = resolve_secrets_path("~/seren/alpha/secrets.json")
        assert got == fake_home / "seren" / "alpha" / "secrets.json"

    def test_not_frozen_at_import(self, fake_home, monkeypatch):
        """A bare load_token() honours an env var set AFTER import."""
        secrets = fake_home / "seren" / "gamma" / "secrets.json"
        secrets.parent.mkdir(parents=True)
        secrets.write_text(json.dumps({"observatory_token": "gamma-tok"}))
        assert load_token() is None
        monkeypatch.setenv(SECRETS_ENV, str(secrets))
        assert load_token() == "gamma-tok"


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
        from seren_observatory.auth import BearerAuthMiddleware

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
        from seren_observatory.auth import BearerAuthMiddleware

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
        (503) even though reads stay open - the unprovisioned-observatory-on-0.0.0.0
        remote-reboot-button guard."""
        from starlette.testclient import TestClient
        c = TestClient(app_no_token, raise_server_exceptions=True)
        r = c.post("/mutate")
        assert r.status_code == 503

    def test_interlock_message_names_the_resolved_path(self, tmp_path):
        """The 503 tells the operator where to put the token. It must be the
        file the observatory reads, not a hard-coded ~/.seren/secrets.json."""
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from seren_observatory.auth import BearerAuthMiddleware

        where = tmp_path / "seren" / "alpha" / "secrets.json"
        app = FastAPI()
        app.add_middleware(BearerAuthMiddleware, expected_token=None,
                           secrets_path=where)

        @app.post("/mutate")
        async def mutate():
            return {"data": "changed"}

        r = TestClient(app).post("/mutate")
        assert r.status_code == 503
        assert str(where) in r.json()["detail"]
        assert "~/.seren/secrets.json" not in r.json()["detail"]

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