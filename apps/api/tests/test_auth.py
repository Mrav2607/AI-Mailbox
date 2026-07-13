"""Auth tests: token round-trip and the 401 boundary on protected endpoints.

These run offline (no DB/Redis). The unauthenticated checks reject the request
in the auth dependency before any database access, so they need no live DB. A
full cross-user authorization test (user A cannot read user B's data) needs a
test database and is a follow-up once test-DB fixtures exist.
"""

from datetime import datetime, timezone

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
from app.main import app

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
