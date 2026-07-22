"""Offline Google OAuth coverage: state binding, PKCE, and account linking.

The route-session double deliberately stages inserts until commit, so the
login tests can prove a failed provider insert does not leave an orphan user.
"""

from __future__ import annotations

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
from app.routes import auth_google

client = TestClient(app)

START = "/api/v1/auth/google/start"
CALLBACK = "/api/v1/auth/google/callback"


def _configure_google(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(
        settings, "google_redirect_uri", "http://localhost:5173/auth/google/callback"
    )


def _redis_down(*args):
    raise redis.ConnectionError("redis is down")


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


def _exchange(email="gmail@example.com", refresh_token="new-refresh", scope="granted"):
    return (email, "access", refresh_token, datetime.now(timezone.utc), scope)


def _status(call) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        call()
    return exc.value


def test_start_stores_login_payload_and_pkce_challenge(monkeypatch):
    _configure_google(monkeypatch)
    stored = []
    monkeypatch.setattr(
        auth_google, "_store_state", lambda state, payload: stored.append((state, payload))
    )

    response = auth_google.google_auth_start()
    query = parse_qs(urlparse(response["auth_url"]).query)

    assert stored[0][0] == query["state"][0]
    assert stored[0][1]["mode"] == "login"
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        auth_google._pkce_challenge(stored[0][1]["pkce_verifier"])
    ]


def test_connect_start_stores_user_bound_payload_and_pkce_challenge(monkeypatch):
    _configure_google(monkeypatch)
    stored = []
    user = _user()
    monkeypatch.setattr(
        auth_google, "_store_state", lambda state, payload: stored.append((state, payload))
    )

    response = auth_google.gmail_connect_start(user)
    query = parse_qs(urlparse(response["auth_url"]).query)

    assert stored[0][1]["mode"] == "connect"
    assert stored[0][1]["user_id"] == str(user.id)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        auth_google._pkce_challenge(stored[0][1]["pkce_verifier"])
    ]


def test_start_fails_closed_when_redis_is_down(monkeypatch):
    _configure_google(monkeypatch)
    monkeypatch.setattr(auth_google, "_store_state", _redis_down)
    assert client.get(START).status_code == 503


def test_callback_rejects_missing_state():
    response = client.get(CALLBACK, params={"code": "abc"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired OAuth state."


def test_callback_rejects_unknown_state(monkeypatch):
    monkeypatch.setattr(auth_google, "_consume_state", lambda state: None)
    response = client.get(CALLBACK, params={"code": "abc", "state": "not-ours"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired OAuth state."


def test_callback_rejects_connect_mode_state(monkeypatch):
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": "other", "pkce_verifier": "v"},
    )
    assert _status(
        lambda: auth_google.google_auth_callback("code", "state", _DB())
    ).status_code == 400


def test_callback_returns_503_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(auth_google, "_consume_state", _redis_down)
    response = client.get(CALLBACK, params={"code": "abc", "state": "whatever"})
    assert response.status_code == 503


def test_exchange_sends_verifier_and_hides_google_error_bodies(monkeypatch):
    """Google error text can echo config, so it stays out of the response."""
    _configure_google(monkeypatch)
    seen = {}

    class FakeResponse:
        status_code = 400
        text = '{"error": "invalid_client", "client_id": "test-client-id"}'

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            seen.update(kwargs["data"])
            return FakeResponse()

    monkeypatch.setattr(auth_google.httpx, "Client", FakeClient)
    error = _status(lambda: auth_google._exchange_code("code", "verifier"))
    assert error.status_code == 400
    assert error.detail == "Google sign-in failed; try again."
    assert seen["code_verifier"] == "verifier"
    assert "client_id" not in error.detail


def test_empty_profile_email_rejects_before_any_account_is_created(monkeypatch):
    _configure_google(monkeypatch)
    db = _DB()
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "login", "pkce_verifier": "verifier"},
    )

    class TokenResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "access"}

    class ProfileResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"emailAddress": ""}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            return TokenResponse()

        def get(self, *args, **kwargs):
            return ProfileResponse()

    monkeypatch.setattr(auth_google.httpx, "Client", FakeClient)
    assert _status(
        lambda: auth_google.google_auth_callback("code", "state", db)
    ).status_code == 400
    assert db.users == db.accounts == []


def test_connect_callback_rejects_login_state_and_other_user(monkeypatch):
    user = _user()
    db = _DB()
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "login", "pkce_verifier": "v"},
    )
    assert _status(
        lambda: auth_google.gmail_connect_callback("code", "state", user, db)
    ).status_code == 400

    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(uuid4()), "pkce_verifier": "v"},
    )
    assert _status(
        lambda: auth_google.gmail_connect_callback("code", "state", user, db)
    ).status_code == 400


def test_connect_stores_granted_scope_and_keeps_pause_without_refresh_token(monkeypatch):
    user = _user()
    existing = ProviderAccount(
        user_id=user.id,
        provider="gmail",
        external_user_id="gmail@example.com",
        access_token="old-access",
        refresh_token="old-refresh",
        sync_paused_at=datetime.now(timezone.utc),
        sync_pause_reason="invalid_grant",
    )
    db = _DB()
    db.accounts.append(existing)
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(
        auth_google,
        "_exchange_code",
        lambda *args: _exchange(refresh_token=None, scope="gmail.readonly"),
    )

    response = auth_google.gmail_connect_callback("code", "state", user, db)

    assert response == {"status": "connected", "provider_email": "gmail@example.com"}
    assert existing.scope == "gmail.readonly"
    assert existing.sync_paused_at is not None
    assert existing.sync_pause_reason == "invalid_grant"


def test_same_address_reconnect_keeps_scope_when_google_returns_none(monkeypatch):
    user = _user()
    existing = ProviderAccount(
        user_id=user.id,
        provider="gmail",
        external_user_id="gmail@example.com",
        access_token="old-access",
        refresh_token="old-refresh",
        scope="stored scope",
    )
    db = _DB()
    db.accounts.append(existing)
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _exchange(scope=None))

    assert auth_google.gmail_connect_callback("code", "state", user, db)["status"] == "connected"
    assert existing.scope == "stored scope"


def test_same_address_reconnect_unpauses_when_google_returns_refresh_token(monkeypatch):
    user = _user()
    existing = ProviderAccount(
        user_id=user.id,
        provider="gmail",
        external_user_id="gmail@example.com",
        access_token="old-access",
        refresh_token="old-refresh",
        sync_paused_at=datetime.now(timezone.utc),
        sync_pause_reason="invalid_grant",
    )
    db = _DB()
    db.accounts.append(existing)
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(
        auth_google, "_exchange_code", lambda *args: _exchange(scope="granted scope")
    )

    assert auth_google.gmail_connect_callback("code", "state", user, db)["status"] == "connected"
    assert existing.refresh_token == "new-refresh"
    assert existing.sync_paused_at is None
    assert existing.sync_pause_reason is None


def test_connect_conflict_rejects_mailbox_owned_by_a_different_account(monkeypatch):
    user = _user()
    db = _DB()
    db.users.append(_user("gmail@example.com"))
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _exchange())

    error = _status(lambda: auth_google.gmail_connect_callback("code", "state", user, db))
    assert error.status_code == 409
    assert "different account" in error.detail


def test_second_gmail_account_connects_successfully(monkeypatch):
    """Multi-account is a supported flow now: connecting a second, different
    Gmail address inserts a second row instead of the old single-account 409."""
    user = _user()
    db = _DB()
    db.accounts.append(
        ProviderAccount(
            user_id=user.id,
            provider="gmail",
            external_user_id="other@gmail.example",
            access_token="access",
        )
    )
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _exchange())

    response = auth_google.gmail_connect_callback("code", "state", user, db)

    assert response == {"status": "connected", "provider_email": "gmail@example.com"}
    assert {a.external_user_id for a in db.accounts} == {
        "other@gmail.example",
        "gmail@example.com",
    }


def test_connect_integrity_error_rechecks_a_conflict(monkeypatch):
    user = _user()
    db = _DB()

    def concurrent_owner():
        db.users.append(_user("gmail@example.com"))

    db.commit_error = concurrent_owner
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _exchange())

    error = _status(lambda: auth_google.gmail_connect_callback("code", "state", user, db))
    assert error.status_code == 409
    assert (
        error.detail
        == "That Gmail account belongs to a different account — sign in with Google instead."
    )


def test_connect_rejects_mailbox_connected_by_a_different_user_before_insert(monkeypatch):
    user = _user()
    other_user = _user("other@example.com")
    db = _DB()
    db.accounts.append(
        ProviderAccount(
            user_id=other_user.id,
            provider="gmail",
            external_user_id="gmail@example.com",
            access_token="access",
        )
    )
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(user.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _exchange())

    error = _status(lambda: auth_google.gmail_connect_callback("code", "state", user, db))

    assert error.status_code == 409
    assert (
        error.detail
        == "That Gmail account belongs to a different account — sign in with Google instead."
    )
    assert db.pending == []


def test_login_provider_failure_rolls_back_new_user(monkeypatch):
    db = _DB()
    db.commit_error = True
    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "login", "pkce_verifier": "v"},
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _exchange())

    assert _status(lambda: auth_google.google_auth_callback("code", "state", db)).status_code == 400
    assert db.users == []
    assert db.accounts == []
