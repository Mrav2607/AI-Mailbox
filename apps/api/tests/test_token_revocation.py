"""Revocation: a token is only good while its version matches the user's.

The kill switch is AppUser.token_version. Every JWT carries the version it was
minted against; bumping the column orphans every token in circulation. There's
no denylist to keep in sync and nothing to lose on a restart -- the check rides
the user row auth already loads.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token, decode_access_token
from app.db.models import AppUser
from app.deps import get_db
from app.main import app as main_app


class _FakeSession:
    """Serves one user by id, which is all get_current_user asks of the DB."""

    def __init__(self, user):
        self.user = user

    def get(self, model, pk):
        return self.user if pk == self.user.id else None


@pytest.fixture
def user():
    u = AppUser(email="revoke@example.com")
    u.id = uuid.uuid4()
    u.display_name = None
    u.token_version = 3
    return u


@pytest.fixture
def client(user):
    main_app.dependency_overrides[get_db] = lambda: _FakeSession(user)
    try:
        yield TestClient(main_app, raise_server_exceptions=False)
    finally:
        main_app.dependency_overrides.clear()


def _me(client, token):
    return client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})


def test_current_version_is_accepted(client, user):
    token = create_access_token(str(user.id), user.token_version)
    assert _me(client, token).status_code == 200


def test_stale_version_is_rejected(client, user):
    """The whole point: a token minted before revoke-all is dead, however
    valid its signature and expiry."""
    token = create_access_token(str(user.id), user.token_version)
    user.token_version += 1  # someone hit revoke-all
    assert _me(client, token).status_code == 401


def test_token_reissued_after_revocation_works(client, user):
    """Revoking must not lock the user out -- signing in again gets a token
    stamped with the new version."""
    user.token_version += 1
    fresh = create_access_token(str(user.id), user.token_version)
    assert _me(client, fresh).status_code == 200


def test_legacy_token_without_version_claim_is_rejected(client, user):
    """Tokens minted before this feature carry no `tv`. Treating a missing
    claim as version 0 would let a pre-revocation token outlive a revoke, so
    decode requires it and everyone signs in once more."""
    import jwt as pyjwt

    from app.core.config import settings
    from app.core.security import JWT_AUDIENCE, JWT_ISSUER

    legacy = pyjwt.encode(
        {
            "sub": str(user.id),
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "iat": 1_700_000_000,
            "exp": 4_100_000_000,
        },
        settings.api_secret,
        algorithm=settings.jwt_algorithm,
    )
    assert _me(client, legacy).status_code == 401


def test_stripping_the_version_claim_breaks_the_signature(user):
    """You can't downgrade your way past the check: `tv` is inside the signed
    payload, so editing it out invalidates the token."""
    token = create_access_token(str(user.id), user.token_version)
    header, _payload, signature = token.split(".")
    import base64
    import json

    forged_payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": str(user.id)}).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(pyjwt_error()):
        decode_access_token(f"{header}.{forged_payload}.{signature}")


def pyjwt_error():
    import jwt as pyjwt

    return pyjwt.PyJWTError


def test_version_is_required_at_issue_time(user):
    """No default. A default would let a new issuer quietly mint version-0
    tokens that survive every revocation."""
    with pytest.raises(TypeError):
        create_access_token(str(user.id))  # type: ignore[call-arg]
