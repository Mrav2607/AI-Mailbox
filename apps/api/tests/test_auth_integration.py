"""Real-Postgres coverage for the OAuth login/connect flows.

Everything in test_oauth_google.py and test_auth_microsoft.py runs against a
hand-rolled `_DB` double that only surfaces rows on "commit" and assigns ids
at add() time. That's fast and fine for the routing logic, but it hid a real
bug: a first-ever login committed a ProviderAccount with a NULL user_id
because the new AppUser was never flushed before its id was read (the real
session runs autoflush=False, so the double's add()-time id assignment never
would have caught it). 761 green unit tests, one orphaned row in prod.

This tier trades speed for honesty: a real SQLAlchemy Session against a real
Postgres, so autoflush timing, NOT NULL/unique constraints, and rollback
semantics are the actual database's, not an approximation of them. It's
opt-in via TEST_DATABASE_URL so the offline suite (and CI runs that don't set
it) stay green without a live database.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models import AppUser, ProviderAccount
from app.routes import auth_google, auth_microsoft

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to run the real-Postgres integration tier",
)


def _configure_microsoft(monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", "test-client-id")
    monkeypatch.setattr(settings, "microsoft_client_secret", "test-client-secret")
    monkeypatch.setattr(
        settings, "microsoft_redirect_uri", "http://localhost:5173/auth/microsoft/callback"
    )


def _google_exchange(email="gmail@example.com", refresh_token="new-refresh", scope="granted"):
    return (email, "access", refresh_token, datetime.now(timezone.utc), scope)


def _microsoft_exchange(
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


@pytest.fixture(scope="session")
def _engine():
    """Build the schema once per test run against a real Postgres.

    Refuses anything but a `*_test` database up front -- every test in this
    module truncates app_user, and a typo'd env var pointing at dev or prod
    must fail loudly here, not quietly wipe real data.
    """
    db_name = urlparse(TEST_DATABASE_URL).path.lstrip("/")
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"TEST_DATABASE_URL points at {db_name!r}, not a *_test database -- "
            "refusing to run tests that TRUNCATE tables against it"
        )
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    # checkfirst (the create_all default) is fine either way: in CI the schema
    # already exists from `alembic upgrade head`, and CI separately proves that
    # schema matches these models. Locally, against a fresh test database, this
    # builds it straight from the models instead.
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(_engine):
    """A real, per-test session with a clean app_user table.

    TRUNCATE ... CASCADE takes provider_account and everything else with an
    FK back to app_user along with it, so each test starts from empty without
    needing to know every downstream table by name.
    """
    session_factory = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    session = session_factory()
    session.execute(text("TRUNCATE app_user CASCADE"))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_first_google_login_creates_user_and_provider_account(db_session, monkeypatch):
    """Regression for the orphan-row bug: against a real, autoflush=False
    session, the callback must flush the new AppUser before building its
    ProviderAccount, or the account commits with a NULL user_id."""
    monkeypatch.setattr(
        auth_google, "_consume_state", lambda state: {"mode": "login", "pkce_verifier": "v"}
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _google_exchange())

    response = auth_google.google_auth_callback("code", "state", db_session)

    user = db_session.query(AppUser).filter(AppUser.email == "gmail@example.com").one()
    account = (
        db_session.query(ProviderAccount).filter(ProviderAccount.provider == "gmail").one()
    )
    assert account.user_id == user.id
    assert response["user"]["id"] == str(user.id)


def test_second_google_login_reuses_the_user_and_upserts_the_account(db_session, monkeypatch):
    """A repeat login for the same identity must not mint a second user or a
    second provider_account row -- it's an upsert, not an insert-only path."""
    monkeypatch.setattr(
        auth_google, "_consume_state", lambda state: {"mode": "login", "pkce_verifier": "v"}
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _google_exchange())

    first = auth_google.google_auth_callback("code", "state", db_session)
    second = auth_google.google_auth_callback("code", "state", db_session)

    assert first["user"]["id"] == second["user"]["id"]
    accounts = (
        db_session.query(ProviderAccount).filter(ProviderAccount.provider == "gmail").all()
    )
    assert len(accounts) == 1


def test_gmail_connect_conflicts_when_the_address_belongs_to_another_user(
    db_session, monkeypatch
):
    """Connecting a Gmail address that already owns a different AppUser must
    409, not silently link -- otherwise account A could hijack account B by
    connecting B's Gmail address."""
    owner = AppUser(email="gmail@example.com")
    signed_in = AppUser(email="signedin@example.com")
    db_session.add_all([owner, signed_in])
    db_session.commit()

    monkeypatch.setattr(
        auth_google,
        "_consume_state",
        lambda state: {"mode": "connect", "user_id": str(signed_in.id), "pkce_verifier": "v"},
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _google_exchange())

    with pytest.raises(HTTPException) as exc:
        auth_google.gmail_connect_callback("code", "state", signed_in, db_session)

    assert exc.value.status_code == 409
    assert "different account" in exc.value.detail


def test_first_microsoft_login_creates_user_and_provider_account(db_session, monkeypatch):
    """Same regression as the Google case, for the Outlook callback -- it has
    its own flush-then-insert path and needs its own proof."""
    _configure_microsoft(monkeypatch)
    monkeypatch.setattr(
        auth_microsoft, "_consume_state", lambda state: {"mode": "login", "pkce_verifier": "v"}
    )
    monkeypatch.setattr(auth_microsoft, "_exchange_code", lambda *args: _microsoft_exchange())

    response = auth_microsoft.microsoft_auth_callback("code", "state", db_session)

    user = db_session.query(AppUser).filter(AppUser.email == "user@outlook.example").one()
    account = (
        db_session.query(ProviderAccount).filter(ProviderAccount.provider == "outlook").one()
    )
    assert account.user_id == user.id
    assert response["user"]["id"] == str(user.id)


def test_provider_account_requires_a_user_id(db_session):
    """Proves the NOT NULL constraint on provider_account.user_id is actually
    live in the schema this test DB built (via create_all, mirroring migration
    0020/0021) -- the exact column the orphan-row bug depended on being
    unenforced."""
    db_session.add(
        ProviderAccount(
            user_id=None,
            provider="gmail",
            external_user_id="gmail@example.com",
            access_token="access",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_google_login_rolls_back_without_leaving_a_half_created_user(db_session, monkeypatch):
    """A login for an identity whose provider row already belongs to another
    user can't win the cross-user unique constraint, so the commit fails --
    real rollback must leave zero trace of the AppUser the callback staged
    for it, not a half-created account the doubles only approximate."""
    owner = AppUser(email="owner@example.com")
    db_session.add(owner)
    db_session.flush()
    db_session.add(
        ProviderAccount(
            user_id=owner.id,
            provider="gmail",
            external_user_id="gmail@example.com",
            access_token="access",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        auth_google, "_consume_state", lambda state: {"mode": "login", "pkce_verifier": "v"}
    )
    monkeypatch.setattr(auth_google, "_exchange_code", lambda *args: _google_exchange())

    before = db_session.query(AppUser).count()
    with pytest.raises(HTTPException) as exc:
        auth_google.google_auth_callback("code", "state", db_session)
    after = db_session.query(AppUser).count()

    assert exc.value.status_code == 400
    assert before == after
