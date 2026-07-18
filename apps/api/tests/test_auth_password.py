"""Offline coverage for the verification-first email/password route flow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.sql.dml import Update

from app.core.security import create_access_token, hash_password
from app.db.models import AppUser
from app.db.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    ResetPasswordRequest,
    ResendVerificationRequest,
    SignupRequest,
    VerifyEmailRequest,
)
from app.deps import get_current_user
from app.routes import auth_password


class _Query:
    """Small query double supporting the legacy query API the routes use."""

    def __init__(self, db, model):
        self.db = db
        self.model = model
        self.filters: dict[str, object] = {}
        self.requires_unexpired = False

    def filter(self, *criteria):
        for criterion in criteria:
            if criterion.left.name == "expires_at":
                self.requires_unexpired = True
                continue
            self.filters[criterion.left.name] = criterion.right.value
        return self

    def with_for_update(self):
        return self

    def first(self):
        rows = self.db.users if self.model is AppUser else self.db.tokens
        for row in rows:
            if all(getattr(row, name) == value for name, value in self.filters.items()) and (
                not self.requires_unexpired or row.expires_at > datetime.now(timezone.utc)
            ):
                return row
        return None


class _DB:
    """In-memory route-session double; no Postgres or broker is needed here."""

    def __init__(self):
        self.users: list[AppUser] = []
        self.tokens: list[SimpleNamespace] = []
        self.commits = 0

    def query(self, model):
        return _Query(self, model)

    def add(self, row):
        if isinstance(row, AppUser):
            row.id = uuid4()
            row.token_version = 0
            self.users.append(row)

    def execute(self, statement):
        if isinstance(statement, Update):
            for user in self.users:
                user.token_version += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        return None

    def refresh(self, row):
        return None

    def get(self, model, user_id):
        if model is AppUser:
            return next((user for user in self.users if user.id == user_id), None)
        return None


class _TokenStore:
    """Route-level token fake that preserves reissue and single-use behavior."""

    def __init__(self, db):
        self.db = db
        self.counter = 0
        self.by_raw: dict[str, SimpleNamespace] = {}

    def issue(
        self,
        db,
        *,
        purpose,
        email,
        user_id=None,
        pending_password_hash=None,
        display_name=None,
        ttl,
    ):
        for raw, row in list(self.by_raw.items()):
            if row.email == email and row.purpose == purpose:
                self.by_raw.pop(raw)
                self.db.tokens.remove(row)
        self.counter += 1
        raw_token = f"{purpose}-token-{self.counter}"
        row = SimpleNamespace(
            purpose=purpose,
            email=email,
            user_id=user_id,
            pending_password_hash=pending_password_hash,
            display_name=display_name,
            expires_at=datetime.now(timezone.utc) + ttl,
        )
        self.by_raw[raw_token] = row
        self.db.tokens.append(row)
        return raw_token

    def consume(self, db, *, purpose, raw_token):
        row = self.by_raw.get(raw_token)
        if row is None or row.purpose != purpose:
            return None
        self.by_raw.pop(raw_token)
        self.db.tokens.remove(row)
        return row


@pytest.fixture
def route_env(monkeypatch):
    db = _DB()
    tokens = _TokenStore(db)
    delay = MagicMock()
    monkeypatch.setattr(auth_password, "issue_token", tokens.issue)
    monkeypatch.setattr(auth_password, "consume_token", tokens.consume)
    monkeypatch.setattr(auth_password.send_auth_email, "delay", delay)
    return db, tokens, delay


def _add_user(db, email, password_hash=None):
    user = AppUser(email=email, password_hash=password_hash)
    user.display_name = None
    user.email_verified_at = None
    db.add(user)
    return user


def _status(call) -> int:
    with pytest.raises(HTTPException) as exc:
        call()
    return exc.value.status_code


def _resolve_current_user(db, token):
    return get_current_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db
    )


def test_signup_is_fixed_and_never_creates_or_changes_a_user(route_env):
    db, _tokens, delay = route_env
    existing = _add_user(db, "exists@example.com", hash_password("original password"))
    original_hash = existing.password_hash

    new_response = auth_password.signup(
        SignupRequest(email="new@example.com", password="new password"), db
    )
    existing_response = auth_password.signup(
        SignupRequest(email="exists@example.com", password="other password"), db
    )

    assert new_response == existing_response == {"status": "verification_sent"}
    assert db.users == [existing]
    assert existing.password_hash == original_hash
    assert delay.call_count == 2


def test_signup_verify_and_login_create_the_password_account(route_env):
    db, tokens, delay = route_env

    signup = auth_password.signup(
        SignupRequest(
            email="new@example.com",
            password="correct password",
            display_name="New User",
        ),
        db,
    )
    raw_token = next(iter(tokens.by_raw))
    verified = auth_password.verify_email(VerifyEmailRequest(token=raw_token), db)
    logged_in = auth_password.login(
        LoginRequest(email="new@example.com", password="correct password"), db
    )

    assert signup == {"status": "verification_sent"}
    assert verified["token_type"] == "bearer"
    assert db.users[0].display_name == "New User"
    assert db.users[0].password_hash is not None
    assert logged_in["token_type"] == "bearer"
    purpose, email, link = delay.call_args.args
    assert (purpose, email) == ("verify_email", "new@example.com")
    assert link.startswith("http://localhost:5173/auth/verify-email#token=")


def test_verify_cannot_overwrite_an_existing_password(route_env):
    db, tokens, _delay = route_env
    original_password = "original password"
    pending_password = "attacker password"
    user = _add_user(db, "owner@example.com", hash_password(original_password))
    raw_token = tokens.issue(
        db,
        purpose="verify_email",
        email=user.email,
        pending_password_hash=hash_password(pending_password),
        display_name="Attacker Name",
        ttl=auth_password.VERIFY_TTL,
    )

    verified_status = _status(
        lambda: auth_password.verify_email(VerifyEmailRequest(token=raw_token), db)
    )
    original_login = auth_password.login(
        LoginRequest(email=user.email, password=original_password), db
    )
    pending_status = _status(
        lambda: auth_password.login(
            LoginRequest(email=user.email, password=pending_password), db
        )
    )

    assert verified_status == 400
    assert raw_token not in tokens.by_raw
    assert original_login["token_type"] == "bearer"
    assert pending_status == 401


def test_login_rejects_unknown_and_google_only_accounts(route_env):
    db, _tokens, _delay = route_env
    _add_user(db, "google-only@example.com")

    unknown = _status(
        lambda: auth_password.login(
            LoginRequest(email="unknown@example.com", password="correct password"), db
        )
    )
    google_only = _status(
        lambda: auth_password.login(
            LoginRequest(email="google-only@example.com", password="correct password"), db
        )
    )

    assert unknown == google_only == 401


def test_verify_and_reset_reject_bad_or_reused_tokens(route_env):
    db, tokens, _delay = route_env
    user = _add_user(db, "reset@example.com", hash_password("original password"))

    assert _status(lambda: auth_password.verify_email(VerifyEmailRequest(token="bad"), db)) == 400
    verify_token = tokens.issue(
        db,
        purpose="verify_email",
        email="verify@example.com",
        pending_password_hash=hash_password("verify password"),
        ttl=auth_password.VERIFY_TTL,
    )
    assert auth_password.verify_email(VerifyEmailRequest(token=verify_token), db)["token_type"] == "bearer"
    assert _status(lambda: auth_password.verify_email(VerifyEmailRequest(token=verify_token), db)) == 400

    assert (
        _status(
            lambda: auth_password.reset_password(
                ResetPasswordRequest(token="bad", new_password="replacement password"), db
            )
        )
        == 400
    )
    reset_token = tokens.issue(
        db,
        purpose="password_reset",
        email=user.email,
        user_id=user.id,
        ttl=auth_password.RESET_TTL,
    )
    assert auth_password.reset_password(
        ResetPasswordRequest(token=reset_token, new_password="replacement password"), db
    )["token_type"] == "bearer"
    assert (
        _status(
            lambda: auth_password.reset_password(
                ResetPasswordRequest(token=reset_token, new_password="replacement password"), db
            )
        )
        == 400
    )


def test_reset_replaces_password_revokes_old_session_and_returns_a_fresh_one(route_env):
    db, tokens, _delay = route_env
    user = _add_user(db, "reset@example.com", hash_password("original password"))
    old_token = create_access_token(str(user.id), user.token_version)
    reset_token = tokens.issue(
        db,
        purpose="password_reset",
        email=user.email,
        user_id=user.id,
        ttl=auth_password.RESET_TTL,
    )

    response = auth_password.reset_password(
        ResetPasswordRequest(token=reset_token, new_password="replacement password"), db
    )

    assert user.token_version == 1
    assert _status(lambda: _resolve_current_user(db, old_token)) == 401
    assert _resolve_current_user(db, response["access_token"]) is user
    assert auth_password.login(
        LoginRequest(email=user.email, password="replacement password"), db
    )["token_type"] == "bearer"


def test_forgot_password_is_fixed_for_known_and_unknown_addresses(route_env):
    db, _tokens, delay = route_env
    known = _add_user(db, "known@example.com", hash_password("correct password"))

    known_response = auth_password.forgot_password(ForgotPasswordRequest(email=known.email), db)
    unknown_response = auth_password.forgot_password(
        ForgotPasswordRequest(email="unknown@example.com"), db
    )

    assert known_response == unknown_response == {"status": "reset_sent"}
    assert delay.call_count == 2
    assert all(call.args[0] == "password_reset" for call in delay.call_args_list)
    assert all("/auth/reset-password#token=" in call.args[2] for call in delay.call_args_list)


def test_resend_is_fixed_and_only_enqueues_for_a_pending_token(route_env):
    db, tokens, delay = route_env

    absent = auth_password.resend_verification(
        ResendVerificationRequest(email="absent@example.com"), db
    )
    tokens.issue(
        db,
        purpose="verify_email",
        email="pending@example.com",
        pending_password_hash=hash_password("pending password"),
        display_name="Pending",
        ttl=auth_password.VERIFY_TTL,
    )
    pending = auth_password.resend_verification(
        ResendVerificationRequest(email="pending@example.com"), db
    )

    assert absent == pending == {"status": "verification_sent"}
    delay.assert_called_once()
    purpose, email, link = delay.call_args.args
    assert (purpose, email) == ("verify_email", "pending@example.com")
    assert "/auth/verify-email#token=" in link


def test_resend_expired_pending_token_is_fixed_and_does_not_enqueue(route_env):
    db, tokens, delay = route_env
    tokens.issue(
        db,
        purpose="verify_email",
        email="expired@example.com",
        pending_password_hash=hash_password("pending password"),
        ttl=timedelta(seconds=-1),
    )

    response = auth_password.resend_verification(
        ResendVerificationRequest(email="expired@example.com"), db
    )

    assert response == {"status": "verification_sent"}
    delay.assert_not_called()
