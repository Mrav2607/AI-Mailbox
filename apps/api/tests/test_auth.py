"""Auth tests: token round-trip and the 401 boundary on protected endpoints.

These run offline (no DB/Redis). The unauthenticated checks reject the request
in the auth dependency before any database access, so they need no live DB. A
full cross-user authorization test (user A cannot read user B's data) needs a
test database and is a follow-up once test-DB fixtures exist.
"""

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token, decode_access_token
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
    token = create_access_token("user-123")
    claims = decode_access_token(token)
    assert claims["sub"] == "user-123"
    assert "exp" in claims and "iat" in claims


def test_expired_token_is_rejected():
    token = create_access_token("user-123", expires_minutes=-1)
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token)


def test_tampered_token_is_rejected():
    token = create_access_token("user-123")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token + "tampered")


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_protected_endpoint_requires_token(path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_protected_endpoint_rejects_garbage_token(path):
    resp = client.get(path, headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
