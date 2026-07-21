"""Auth tests: token round-trip and the 401 boundary on protected endpoints.

These run offline (no DB/Redis). The unauthenticated checks reject the request
in the auth dependency before any database access, so they need no live DB. A
full cross-user authorization test (user A cannot read user B's data) needs a
test database and is a follow-up once test-DB fixtures exist.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import (
    JWT_AUDIENCE,
    JWT_ISSUER,
    create_access_token,
    decode_access_token,
)
from app.deps import get_current_user, get_db
from app.main import app
from app.routes import auth as auth_routes

client = TestClient(app)

# Endpoints that must reject unauthenticated callers.
PROTECTED_GETS = [
    "/api/v1/mail/triage",
    "/api/v1/analytics/overview",
    "/api/v1/auth/connections",
    "/api/v1/auth/me",
]


def test_token_roundtrip():
    token = create_access_token("user-123", 0)
    claims = decode_access_token(token)
    assert claims["sub"] == "user-123"
    assert "exp" in claims and "iat" in claims
    assert claims["iss"] == JWT_ISSUER
    assert claims["aud"] == JWT_AUDIENCE


def test_expired_token_is_rejected():
    token = create_access_token("user-123", 0, expires_minutes=-1)
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token)


def test_tampered_token_is_rejected():
    token = create_access_token("user-123", 0)
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token + "tampered")


def _craft_token(claims: dict) -> str:
    """Sign arbitrary claims with our real secret, bypassing create_access_token,
    so we can test tokens that are validly signed but missing/wrong on claims."""
    return jwt.encode(claims, settings.api_secret, algorithm=settings.jwt_algorithm)


def test_token_without_audience_is_rejected():
    now = int(datetime.now(timezone.utc).timestamp())
    token = _craft_token(
        {"sub": "user-123", "iss": JWT_ISSUER, "iat": now, "exp": now + 300}
    )
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token)


def test_token_with_wrong_issuer_is_rejected():
    now = int(datetime.now(timezone.utc).timestamp())
    token = _craft_token(
        {
            "sub": "user-123",
            "iss": "some-other-service",
            "aud": JWT_AUDIENCE,
            "iat": now,
            "exp": now + 300,
        }
    )
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token)


def test_token_without_exp_is_rejected():
    """A token stripped of exp must fail the required-claims check -- otherwise
    it'd never expire."""
    now = int(datetime.now(timezone.utc).timestamp())
    token = _craft_token(
        {"sub": "user-123", "iss": JWT_ISSUER, "aud": JWT_AUDIENCE, "iat": now}
    )
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token)


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_protected_endpoint_requires_token(path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_protected_endpoint_rejects_garbage_token(path):
    resp = client.get(path, headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


@pytest.mark.parametrize("env", ["prod", "production", "staging"])
def test_demo_login_is_hidden_in_production(monkeypatch, env):
    """demo-login verifies no credential, so in any non-dev environment it must
    404 before touching the database (which is why this test can run offline)."""
    monkeypatch.setattr(settings, "app_env", env)
    resp = client.post("/api/v1/auth/demo-login", json={"email": "a@example.com"})
    assert resp.status_code == 404


USER_ID = uuid4()
CONNECTIONS_URL = "/api/v1/auth/connections"


def _connection(**overrides):
    defaults = dict(
        id=uuid4(),
        user_id=USER_ID,
        provider="gmail",
        created_at=datetime.now(timezone.utc),
        external_user_id="user@gmail.example",
        sync_paused_at=None,
        refresh_token="a-refresh-token",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def auth_client():
    user = SimpleNamespace(id=USER_ID)
    db = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db
    app.dependency_overrides.clear()


def test_connections_list_carries_email_address_and_reauth_flag(auth_client):
    api_client, db = auth_client
    healthy = _connection()
    paused = _connection(sync_paused_at=datetime.now(timezone.utc))
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
        healthy,
        paused,
    ]

    body = api_client.get(CONNECTIONS_URL).json()

    assert body["connections"][0]["email_address"] == healthy.external_user_id
    assert body["connections"][0]["reauth_required"] is False
    assert body["connections"][1]["email_address"] == paused.external_user_id
    assert body["connections"][1]["reauth_required"] is True


def test_delete_connection_returns_204_for_own_account(auth_client, monkeypatch):
    api_client, db = auth_client
    connection = _connection()
    db.get.return_value = connection
    monkeypatch.setattr(auth_routes, "_revoke_google_token", lambda token: None)

    resp = api_client.delete(f"{CONNECTIONS_URL}/{connection.id}")

    assert resp.status_code == 204
    db.delete.assert_called_once_with(connection)
    db.commit.assert_called_once()


def test_delete_connection_404s_for_an_unknown_id(auth_client):
    api_client, db = auth_client
    db.get.return_value = None

    resp = api_client.delete(f"{CONNECTIONS_URL}/{uuid4()}")

    assert resp.status_code == 404
    db.delete.assert_not_called()


def test_delete_connection_404s_for_another_users_account(auth_client):
    # 404, not 403 -- same pattern as delete_thread, so we don't confirm to
    # the caller that a connection with this id exists at all.
    api_client, db = auth_client
    someone_elses = _connection(user_id=uuid4())
    db.get.return_value = someone_elses

    resp = api_client.delete(f"{CONNECTIONS_URL}/{someone_elses.id}")

    assert resp.status_code == 404
    db.delete.assert_not_called()


def test_delete_connection_still_succeeds_when_google_revocation_fails(
    auth_client, monkeypatch
):
    # Revocation is best-effort; a dead network to Google must never block the
    # delete of our own row.
    api_client, db = auth_client
    connection = _connection()
    db.get.return_value = connection

    def exploding_post(*args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(auth_routes.httpx, "post", exploding_post)

    resp = api_client.delete(f"{CONNECTIONS_URL}/{connection.id}")

    assert resp.status_code == 204
    db.delete.assert_called_once_with(connection)
