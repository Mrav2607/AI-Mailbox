"""Demo seeding against a real Postgres.

Same idiom as test_feedback_integration.py: opt-in via TEST_DATABASE_URL so the
default offline run stays fast. A real database matters here -- the seed writes
across app_user, provider_account, mail_thread, mail_message, and
classification, and the thing worth protecting is that a fresh `docker compose
up` ends at a populated console instead of an empty one.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import AppUser, Classification, MailMessage, MailThread, ProviderAccount
from app.scripts.seed_demo import LEGACY_DEMO_EXTERNAL_ID, seed_demo_data

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to run the real-Postgres integration tier",
)


@pytest.fixture(scope="session")
def _engine():
    db_name = urlparse(TEST_DATABASE_URL).path.lstrip("/")
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"TEST_DATABASE_URL points at {db_name!r}, not a *_test database -- "
            "refusing to run tests that TRUNCATE tables against it"
        )
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(_engine):
    session_factory = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    session = session_factory()
    session.execute(text("TRUNCATE app_user CASCADE"))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def user(db_session):
    row = AppUser(email="seeded@example.com", display_name="Seeded")
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_seed_populates_threads_messages_and_labels(db_session, user):
    created = seed_demo_data(db_session, user)

    assert created > 0
    threads = db_session.execute(
        select(func.count()).select_from(MailThread).where(MailThread.user_id == user.id)
    ).scalar_one()
    messages = db_session.execute(select(func.count()).select_from(MailMessage)).scalar_one()
    assert threads == created
    assert messages == created


def test_seed_spans_multiple_buckets_and_confidence_tiers(db_session, user):
    """A demo where everything is one label and one confidence shows off none of
    the UI -- the buckets and the confidence ramp both need real spread."""
    seed_demo_data(db_session, user)

    labels = set(
        db_session.execute(select(Classification.label).distinct()).scalars().all()
    )
    assert len(labels - {None}) >= 4

    confidences = [
        c
        for c in db_session.execute(select(Classification.confidence)).scalars().all()
        if c is not None
    ]
    # Low tier paints at <=35%, high tier at >=81%: cover both ends.
    assert min(confidences) <= 0.35
    assert max(confidences) >= 0.81


def test_seed_leaves_one_thread_unclassified(db_session, user):
    """The unclassified bucket should not be mysteriously empty in the demo."""
    seed_demo_data(db_session, user)

    threads = db_session.execute(select(func.count()).select_from(MailThread)).scalar_one()
    classified = db_session.execute(
        select(func.count()).select_from(Classification)
    ).scalar_one()
    assert classified == threads - 1


def test_seed_is_idempotent(db_session, user):
    first = seed_demo_data(db_session, user)
    second = seed_demo_data(db_session, user)

    assert second == 0
    threads = db_session.execute(select(func.count()).select_from(MailThread)).scalar_one()
    assert threads == first


def test_demo_account_is_invisible_to_the_sync_scheduler(db_session, user):
    """The periodic sync only picks up accounts with a refresh token, and the UI
    flags reconnection off sync_paused_at. The placeholder connection must trip
    neither -- no Gmail calls for fake credentials, no bogus 'reconnect' badge.
    """
    seed_demo_data(db_session, user)

    account = db_session.execute(
        select(ProviderAccount).where(ProviderAccount.user_id == user.id)
    ).scalar_one()
    assert account.refresh_token is None
    assert account.sync_paused_at is None


def test_seeding_two_different_users_does_not_collide(db_session, user):
    """provider_account has a (provider, external_user_id) unique constraint
    that ISN'T scoped by user_id (models/provider.py) -- a shared literal
    external id used to make the SECOND demo user's insert violate it. Two
    users seeding back to back must not raise, and each must get their own,
    differently-identified account.
    """
    other = AppUser(email="seeded-2@example.com", display_name="Seeded Two")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    created_first = seed_demo_data(db_session, user)
    created_second = seed_demo_data(db_session, other)

    assert created_first > 0
    assert created_second > 0

    account_first = db_session.execute(
        select(ProviderAccount).where(ProviderAccount.user_id == user.id)
    ).scalar_one()
    account_second = db_session.execute(
        select(ProviderAccount).where(ProviderAccount.user_id == other.id)
    ).scalar_one()
    assert account_first.external_user_id != account_second.external_user_id


def test_legacy_external_id_account_is_found_not_duplicated(db_session, user):
    """A user seeded before the external id was scoped per-user has a
    provider_account row under the old shared literal. Seeding again must
    find that row (and hang new threads off it) instead of inserting a
    second placeholder account -- which would double-hit the (provider,
    external_user_id) constraint problem this fix exists to close.
    """
    legacy_account = ProviderAccount(
        user_id=user.id,
        provider="gmail",
        external_user_id=LEGACY_DEMO_EXTERNAL_ID,
        access_token="demo-seed-no-token",
        refresh_token=None,
    )
    db_session.add(legacy_account)
    db_session.commit()

    created = seed_demo_data(db_session, user)

    assert created > 0
    accounts = db_session.execute(
        select(ProviderAccount).where(ProviderAccount.user_id == user.id)
    ).scalars().all()
    assert len(accounts) == 1
    assert accounts[0].external_user_id == LEGACY_DEMO_EXTERNAL_ID
    threads = db_session.execute(
        select(func.count())
        .select_from(MailThread)
        .where(MailThread.provider_account_id == accounts[0].id)
    ).scalar_one()
    assert threads == created
