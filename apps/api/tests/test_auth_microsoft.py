"""Offline Microsoft OAuth coverage: state binding, PKCE, tid:oid identity,
existing-account login, connect conflicts, and the unconfigured 503 gate.

Mirrors test_oauth_google.py's shape: a transaction-aware DB double that only
surfaces rows on commit, and httpx/Redis replaced with small fakes so nothing
here needs a live network or Postgres.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
import redis
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.models import AppUser, ProviderAccount
from app.main import app
from app.routes import auth_microsoft

client = TestClient(app)

START = "/api/v1/auth/microsoft/start"
CALLBACK = "/api/v1/auth/microsoft/callback"


def _configure_microsoft(monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", "test-client-id")
    monkeypatch.setattr(settings, "microsoft_client_secret", "test-client-secret")
    monkeypatch.setattr(
        settings, "microsoft_redirect_uri", "http://localhost:5173/auth/microsoft/callback"
    )


def _unconfigure_microsoft(monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", None)
    monkeypatch.setattr(settings, "microsoft_client_secret", None)
    monkeypatch.setattr(settings, "microsoft_redirect_uri", None)


def _redis_down(*args):
    raise redis.ConnectionError("redis is down")


def _b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode("ascii")


def _id_token(tid: str = "tenant-1", oid: str = "object-1") -> str:
    """A JWT-shaped string with a real payload segment and dummy header/sig --
    _decode_id_token_claims never checks the signature."""
    return f"{_b64url({'alg': 'none'})}.{_b64url({'tid': tid, 'oid': oid})}.sig"


class _Query:
    """Small query double supporting the equality filters in this router."""

    def __init__(self, db, model):
        self.db = db
        self.model = model
        self.filters: dict[str, tuple[object, bool]] = {}

    def filter(self, *criteria):
        for criterion in criteria:
            column = criterion.left
            lowered = getattr(column, "name", None) == "lower"
            if lowered:
                column = next(iter(column.clauses))
            self.filters[column.name] = (criterion.right.value, lowered)
        return self

    def first(self):
        for row in self.db.rows(self.model):
            if all(
                (
                    getattr(row, name).lower() if lowered else getattr(row, name)
                )
                == value
                for name, (value, lowered) in self.filters.items()
            ):
                return row
        return None


class _DB:
    """A transaction-aware route double; stored rows survive only commits."""

    def __init__(self):
        self.users: list[AppUser] = []
        self.accounts: list[ProviderAccount] = []
        self.pending: list[AppUser | ProviderAccount] = []
        self.commit_error = None

    def rows(self, model):
        if model is AppUser:
            return self.users
        if model is ProviderAccount:
            return self.accounts
        raise AssertionError(f"unexpected model: {model}")

    def query(self, model):
        return _Query(self, model)

    def add(self, row):
        if isinstance(row, AppUser):
            row.id = uuid4()
            row.token_version = 0
        self.pending.append(row)

    def commit(self):
        if self.commit_error is not None:
            error = self.commit_error
            self.commit_error = None
            if callable(error):
                error()
            raise IntegrityError("insert", {}, Exception("constraint"))
        for row in self.pending:
            if isinstance(row, AppUser):
                self.users.append(row)
            else:
                self.accounts.append(row)
        self.pending.clear()

    def rollback(self):
        self.pending.clear()


def _user(email="owner@example.com"):
    user = AppUser(email=email)
    user.id = uuid4()
    user.token_version = 0
    return user


def _exchange(
    external_user_id="tenant-1:object-1",
    display_email="user@outlook.example",
    refresh_token="new-refresh",
    scope="granted",
):
    return (
        external_user_id,
        "access",
        refresh_token,
        datetime.now(timezone.utc),
        scope,
        display_email,
    )


def _status(call) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        call()
    return exc.value


# --- 503 unconfigured gate on all four routes ------------------------------


def test_start_returns_503_when_not_configured(monkeypatch):
    _unconfigure_microsoft(monkeypatch)
    response = client.get(START)
    assert response.status_code == 503
    assert response.json()["detail"] == "microsoft oauth not configured"


def test_callback_returns_503_when_not_configured(monkeypatch):
    _unconfigure_microsoft(monkeypatch)
    response = client.get(CALLBACK, params={"code": "abc", "state": "whatever"})
    assert response.status_code == 503
    assert response.json()["detail"] == "microsoft oauth not configured"


def test_connect_start_returns_503_when_not_configured(monkeypatch):
    _unconfigure_microsoft(monkeypatch)
    user = _user()
    error = _status(lambda: auth_microsoft.outlook_connect_start(user))
    assert error.status_code == 503
    assert error.detail == "microsoft oauth not configured"


def test_connect_callback_returns_503_when_not_configured(monkeypatch):
    _unconfigure_microsoft(monkeypatch)
    user = _user()
    db = _DB()
    error = _status(
        lambda: auth_microsoft.outlook_connect_callback(None, None, user, db)
    )
    assert error.status_code == 503
    assert error.detail == "microsoft oauth not configured"


# --- state store + PKCE ------------------------------------------------------


def test_start_stores_login_payload_and_pkce_challenge(monkeypatch):
    _configure_microsoft(monkeypatch)
    stored = []
    monkeypatch.setattr(
        auth_microsoft, "_store_state", lambda state, payload: stored.append((state, payload))
    )

    response = auth_microsoft.microsoft_auth_start()
    query = parse_qs(urlparse(response["auth_url"]).query)

    assert stored[0][0] == query["state"][0]
    assert stored[0][1]["mode"] == "login"
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        auth_microsoft._pkce_challenge(stored[0][1]["pkce_verifier"])
    ]


def test_connect_start_stores_user_bound_payload_and_pkce_challenge(monkeypatch):
    _configure_microsoft(monkeypatch)
    stored = []
    user = _user()
    monkeypatch.setattr(
        auth_microsoft, "_store_state", lambda state, payload: stored.append((state, payload))
    )

    response = auth_microsoft.outlook_connect_start(user)
    query = parse_qs(urlparse(response["auth_url"]).query)

    assert stored[0][1]["mode"] == "connect"
    assert stored[0][1]["user_id"] == str(user.id)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        auth_microsoft._pkce_challenge(stored[0][1]["pkce_verifier"])
    ]


def test_start_fails_closed_when_redis_is_down(monkeypatch):
    _configure_microsoft(monkeypatch)
    monkeypatch.setattr(auth_microsoft, "_store_state", _redis_down)
    assert client.get(START).status_code == 503


def test_state_consumption_is_one_time(monkeypatch):
    """GETDEL semantics: a state that's been consumed can't be replayed."""

    class _FakeRedisStore:
        def __init__(self):
            self.values: dict[str, bytes] = {}

        def set(self, key, value, ex=None):
            self.values[key] = value.encode("utf-8")

        def getdel(self, key):
            return self.values.pop(key, None)

    fake = _FakeRedisStore()
    monkeypatch.setattr(auth_microsoft, "_state_store", lambda: fake)

    auth_microsoft._store_state("state-1", {"mode": "login", "pkce_verifier": "v"})

    first = auth_microsoft._consume_state("state-1")
    assert first == {"mode": "login", "pkce_verifier": "v"}

    second = auth_microsoft._consume_state("state-1")
    assert second is None


def test_callback_rejects_missing_state(monkeypatch):
    _configure_microsoft(monkeypatch)
    response = client.get(CALLBACK, params={"code": "abc"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired OAuth state."


def test_callback_rejects_unknown_state(monkeypatch):
    _configure_microsoft(monkeypatch)
    monkeypatch.setattr(auth_microsoft, "_consume_state", lambda state: None)
    response = client.get(CALLBACK, params={"code": "abc", "state": "not-ours"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired OAuth state."


def test_callback_returns_503_when_redis_is_down(monkeypatch):
    _configure_microsoft(monkeypatch)
    monkeypatch.setattr(auth_microsoft, "_consume_state", _redis_down)
    response = client.get(CALLBACK, params={"code": "abc", "state": "whatever"})
    assert response.status_code == 503


# --- tid:oid identity + display_email ---------------------------------------


def test_exchange_code_builds_tid_oid_identity_and_normalizes_display_email(monkeypatch):
    _configure_microsoft(monkeypatch)

    class _TokenResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "scope": "openid profile",
                "expires_in": 3600,
                "id_token": _id_token(tid="tenant-abc", oid="object-xyz"),
            }

    class _FakeHttpClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            return _TokenResponse()

    class _FakeOutlookClient:
        def __init__(self, token):
            self.token = token

        def get_me(self):
            return {"mail": "  User@Example.com  "}

    monkeypatch.setattr(auth_microsoft.httpx, "Client", _FakeHttpClient)
    monkeypatch.setattr(auth_microsoft, "OutlookClient", _FakeOutlookClient)

    (
        external_user_id,
        access_token,
        refresh_token,
        token_expiry,
        granted_scope,
        display_email,
    ) = auth_microsoft._exchange_code("code", "verifier")

    assert external_user_id == "tenant-abc:object-xyz"
    assert access_token == "access-token"
    assert refresh_token == "refresh-token"
    assert token_expiry is not None
    assert granted_scope == "openid profile"
    assert display_email == "user@example.com"


def test_exchange_code_falls_back_to_user_principal_name(monkeypatch):
    _configure_microsoft(monkeypatch)

    class _TokenResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "access_token": "access-token",
                "id_token": _id_token(),
            }

    class _FakeHttpClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            return _TokenResponse()

    class _FakeOutlookClient:
        def __init__(self, token):
            pass

        def get_me(self):
            return {"mail": None, "userPrincipalName": "Fallback@Example.com"}

    monkeypatch.setattr(auth_microsoft.httpx, "Client", _FakeHttpClient)
    monkeypatch.setattr(auth_microsoft, "OutlookClient", _FakeOutlookClient)

    result = auth_microsoft._exchange_code("code", "verifier")
    assert result[5] == "fallback@example.com"


# --- existing-account login path --------------------------------------------


def test_callback_logs_into_existing_account_owner_without_duplicate_user(monkeypatch):
    """A tid:oid match to an already-connected account wins over matching by
    display email -- the owner logs in even if their login email differs from
    the mailbox's display email, and no second AppUser is created."""
    _configure_microsoft(monkeypatch)
    owner = _user("owner@example.com")
    existing = ProviderAccount(
        user_id=owner.id,
        provider="outlook",
        external_user_id="tenant-1:object-1",
        access_token="old-access",
        refresh_token="old-refresh",
    )
    db = _DB()
    db.users.append(owner)
    db.accounts.append(existing)

    monkeypatch.setattr(
        auth_microsoft,
        "_consume_state",
        lambda state: {"mode": "login", "pkce_verifier": "v"},
    )
    monkeypatch.setattr(
        auth_microsoft,
        "_exchange_code",
        lambda *args: _exchange(
            external_user_id="tenant-1:object-1", display_email="mailbox@outlook.example"
        ),
    )

    response = auth_microsoft.microsoft_auth_callback("code", "state", db)

    assert response["user"]["id"] == str(owner.id)
    assert response["user"]["email"] == "owner@example.com"
    assert len(db.users) == 1
    assert existing.access_token == "access"
    assert existing.refresh_token == "new-refresh"


def test_callback_creates_user_by_display_email_when_no_existing_account(monkeypatch):
    _configure_microsoft(monkeypatch)
    db = _DB()
    monkeypatch.setattr(
        auth_microsoft,
        "_consume_state",
        lambda state: {"mode": "login", "pkce_verifier": "v"},
    )
    monkeypatch.setattr(
        auth_microsoft,
        "_exchange_code",
        lambda *args: _exchange(display_email="new@outlook.example"),
    )

    response = auth_microsoft.microsoft_auth_callback("code", "state", db)

    assert response["user"]["email"] == "new@outlook.example"
    assert len(db.users) == 1
    assert db.accounts[0].external_user_id == "tenant-1:object-1"
    assert db.accounts[0].provider == "outlook"


def test_login_provider_failure_rolls_back_new_user(monkeypatch):
    _configure_microsoft(monkeypatch)
    db = _DB()
    db.commit_error = True
    monkeypatch.setattr(
        auth_microsoft,
        "_consume_state",
        lambda state: {"mode": "login", "pkce_verifier": "v"},
    )
    monkeypatch.setattr(auth_microsoft, "_exchange_code", lambda *args: _exchange())

    assert (
        _status(lambda: auth_microsoft.microsoft_auth_callback("code", "state", db)).status_code
        == 400
    )
    assert db.users == []
    assert db.accounts == []


# --- connect conflicts -------------------------------------------------------


def test_connect_callback_rejects_login_state_and_other_user(monkeypatch):
    _configure_microsoft(monkeypatch)
    user = _user()
    db = _DB()
    monkeypatch.setattr(
        auth_microsoft,
        "_consume_state",
        lambda state: {"mode": "login", "pkce_verifier": "v"},
    )
    assert (
        _status(
            lambda: auth_microsoft.outlook_connect_callback("code", "state", user, db)
        ).status_code
        == 400
    )

    monkeypatch.setattr(
        auth_microsoft,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(uuid4()), "pkce_verifier": "v"},
    )
    assert (
        _status(
            lambda: auth_microsoft.outlook_connect_callback("code", "state", user, db)
        ).status_code
        == 400
    )


def test_connect_conflict_rejects_mailbox_owned_by_a_different_account(monkeypatch):
    _configure_microsoft(monkeypatch)
    user = _user()
    db = _DB()
    db.users.append(_user("mailbox@outlook.example"))
    monkeypatch.setattr(
        auth_microsoft,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(
        auth_microsoft,
        "_exchange_code",
        lambda *args: _exchange(display_email="mailbox@outlook.example"),
    )

    error = _status(
        lambda: auth_microsoft.outlook_connect_callback("code", "state", user, db)
    )
    assert error.status_code == 409
    assert "different account" in error.detail


def test_connect_conflict_rejects_account_connected_by_a_different_user(monkeypatch):
    _configure_microsoft(monkeypatch)
    user = _user()
    other_user = _user("other@example.com")
    db = _DB()
    db.accounts.append(
        ProviderAccount(
            user_id=other_user.id,
            provider="outlook",
            external_user_id="tenant-1:object-1",
            access_token="access",
        )
    )
    monkeypatch.setattr(
        auth_microsoft,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(auth_microsoft, "_exchange_code", lambda *args: _exchange())

    error = _status(
        lambda: auth_microsoft.outlook_connect_callback("code", "state", user, db)
    )
    assert error.status_code == 409
    assert db.pending == []


def test_connect_stores_granted_scope_and_display_email(monkeypatch):
    _configure_microsoft(monkeypatch)
    user = _user()
    db = _DB()
    monkeypatch.setattr(
        auth_microsoft,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(
        auth_microsoft,
        "_exchange_code",
        lambda *args: _exchange(display_email="mailbox@outlook.example", scope="Mail.ReadWrite"),
    )

    response = auth_microsoft.outlook_connect_callback("code", "state", user, db)

    assert response == {"status": "connected", "provider_email": "mailbox@outlook.example"}
    assert db.accounts[0].provider == "outlook"
    assert db.accounts[0].scope == "Mail.ReadWrite"
    assert db.accounts[0].display_email == "mailbox@outlook.example"


def test_connect_integrity_error_rechecks_a_conflict(monkeypatch):
    _configure_microsoft(monkeypatch)
    user = _user()
    db = _DB()

    def concurrent_owner():
        db.users.append(_user("mailbox@outlook.example"))

    db.commit_error = concurrent_owner
    monkeypatch.setattr(
        auth_microsoft,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(
        auth_microsoft,
        "_exchange_code",
        lambda *args: _exchange(display_email="mailbox@outlook.example"),
    )

    error = _status(
        lambda: auth_microsoft.outlook_connect_callback("code", "state", user, db)
    )
    assert error.status_code == 409
