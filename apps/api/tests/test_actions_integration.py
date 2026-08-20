"""Real-Postgres coverage for the agenda cursor pagination work (plan:
docs/plans/2026-08-19-agenda-cursor-pagination-plan.md, item 10).

Same idiom as test_auth_integration.py/test_feedback_integration.py: opt-in
via TEST_DATABASE_URL so the offline suite stays green without a live
database, schema built with `Base.metadata.create_all` against a
`*_test`-only database, one truncated session per test. What the MagicMock
route suite (test_actions_routes.py) can't prove and this tier does: (a) the
REAL extraction path (`extraction_run._claim_extract_record`, the engine
`run_extraction_for_message`/`run_extraction_sweep` both wrap) locks the
thread, derives `user_id` off it, and only then claims the item -- so the
item's `user_id` actually equals its thread's owner via that derivation, not
just via whatever a test happens to hand `claim_action_item` directly. This
is the D6 "application convention, not a DB invariant" the new index/
predicate leans on. And (b) a real cursor-walk over real Postgres ordering,
including the NULLS LAST boundary, duplicate sort keys spanning a page
break, and a row completing mid-walk.

Route functions are called directly (`actions.list_actions(...)`), not
through TestClient -- same choice test_auth_integration.py makes, since the
`Query(...)` defaults on the route's parameters only matter for FastAPI's own
dependency injection, and every call here supplies explicit values anyway.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    ActionItem,
    AppUser,
    Classification,
    MailMessage,
    MailThread,
    ProviderAccount,
)
from app.routes import actions
from app.services.nlp import extraction_run
from app.services.nlp.extractor import ExtractionAttempt, NoAction
from app.services.nlp.providers import CallContext, LlmCredential

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to run the real-Postgres integration tier",
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
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(_engine):
    """A real, per-test session with a clean app_user table.

    TRUNCATE ... CASCADE takes provider_account, mail_thread, mail_message,
    classification, and action_item with it, so each test starts from empty
    without needing to know every downstream table by name.
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


# ---------------------------------------------------------------------------
# Fixture builders -- minimal rows, just enough to satisfy the FK chain.
# ---------------------------------------------------------------------------


def _seed_user_and_account(db, *, email="op@example.com"):
    user = AppUser(email=email)
    db.add(user)
    db.flush()
    account = ProviderAccount(
        user_id=user.id,
        provider="gmail",
        external_user_id=email,
        access_token="access",
    )
    db.add(account)
    db.flush()
    return user, account


def _seed_thread(db, user, account, *, subject="s"):
    thread = MailThread(
        user_id=user.id,
        provider_account_id=account.id,
        provider="gmail",
        provider_thread_id=str(uuid4()),
        subject=subject,
    )
    db.add(thread)
    db.flush()
    return thread


def _seed_visible_message(db, thread, *, label="needs_reply"):
    """A message with a classification in ACTION_LABELS -- the join
    `_visibility_predicates` requires to make an action item agenda-visible
    at all."""
    message = MailMessage(
        thread_id=thread.id,
        provider_message_id=str(uuid4()),
        snippet="hi",
        body_text="there",
    )
    db.add(message)
    db.flush()
    db.add(
        Classification(message_id=message.id, label=label, confidence=0.9, model_version="test")
    )
    db.flush()
    return message


def _seed_action_item(
    db,
    thread,
    *,
    due_at,
    created_at,
    status="open",
    label="needs_reply",
    outcome="extracted",
):
    message = _seed_visible_message(db, thread, label=label)
    item = ActionItem(
        message_id=message.id,
        thread_id=thread.id,
        user_id=thread.user_id,
        outcome=outcome,
        status=status,
        due_at=due_at,
        created_at=created_at,
    )
    db.add(item)
    db.flush()
    return item


def _t(offset_minutes):
    return datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=offset_minutes)


def _expected_order(items):
    """The Python-side equivalent of `due_at ASC NULLS LAST, created_at DESC,
    id DESC`, computed from what we seeded (not from a DB round-trip) so the
    pagination-walk test has an independent ground truth to compare against.

    Three stable passes, least-significant key first -- Python's sort is
    stable, so each later pass only reorders ties the previous pass left
    alone, which is exactly how a multi-column ORDER BY resolves."""
    step = sorted(items, key=lambda i: i.id, reverse=True)
    step = sorted(step, key=lambda i: i.created_at, reverse=True)
    step = sorted(step, key=lambda i: (i.due_at is None, i.due_at))
    return step


def _walk_all_pages(db, user, *, status="open", limit=3):
    """Drive the route's own pager to exhaustion, returning the concatenated
    items (in the order the route returned them) and asserting the final
    page's `next_cursor` came back null."""
    collected = []
    cursor = None
    for _ in range(50):  # generous bound -- a real bug here must not hang the suite
        result = actions.list_actions(
            status=status, limit=limit, cursor=cursor, current_user=user, db=db
        )
        collected.extend(result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("pagination walk did not terminate within 50 pages")
    return collected


# ---------------------------------------------------------------------------
# (a) writer stamps ActionItem.user_id == its thread's user
# ---------------------------------------------------------------------------


def test_extraction_run_stamps_action_item_user_id_from_the_locked_thread(
    db_session, monkeypatch
):
    """Goes through `extraction_run._claim_extract_record` -- the real
    engine both `run_extraction_for_message` and `run_extraction_sweep`
    wrap -- instead of handing `thread.user_id` straight to
    `claim_action_item`. That's the actual production derivation this
    proves: lock the thread, read `user_id` off THAT row, then claim.
    `extract_action_with_usage` is monkeypatched to a canned `NoAction`
    result so the test stays a pure-DB assertion -- no outbound LLM call,
    and no need to resolve a real BYOK credential first."""
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    message = _seed_visible_message(db_session, thread)

    monkeypatch.setattr(
        extraction_run,
        "extract_action_with_usage",
        lambda **_kwargs: ExtractionAttempt(
            result=NoAction(), provider_call_succeeded=True, usage=None
        ),
    )
    stub_credential = LlmCredential(
        provider="openai", base_url="https://example.invalid/v1", api_key="k", model="m"
    )

    bucket, derived_user_id = extraction_run._claim_extract_record(
        db_session,
        message.id,
        call_context=CallContext(credential=stub_credential, payer="operator"),
    )
    db_session.commit()

    assert bucket == "no_action"
    assert derived_user_id == user.id
    row = db_session.execute(
        select(ActionItem).where(ActionItem.message_id == message.id)
    ).scalar_one()
    assert row.user_id == thread.user_id == user.id


# ---------------------------------------------------------------------------
# (b) real-Postgres pagination walk
# ---------------------------------------------------------------------------


def test_pagination_walk_covers_every_row_exactly_once_across_the_null_boundary(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)

    seeded = [
        # Non-null-due segment, including a duplicate (due_at, created_at)
        # pair -- resolved only by the id DESC tiebreak.
        _seed_action_item(db_session, thread, due_at=_t(0), created_at=_t(-10)),
        _seed_action_item(db_session, thread, due_at=_t(0), created_at=_t(-10)),
        _seed_action_item(db_session, thread, due_at=_t(60), created_at=_t(-11)),
        _seed_action_item(db_session, thread, due_at=_t(120), created_at=_t(-12)),
        _seed_action_item(db_session, thread, due_at=_t(120), created_at=_t(-12)),
        # Null-due segment, also with a duplicate created_at pair.
        _seed_action_item(db_session, thread, due_at=None, created_at=_t(-20)),
        _seed_action_item(db_session, thread, due_at=None, created_at=_t(-20)),
        _seed_action_item(db_session, thread, due_at=None, created_at=_t(-21)),
        _seed_action_item(db_session, thread, due_at=None, created_at=_t(-22)),
        _seed_action_item(db_session, thread, due_at=None, created_at=_t(-22)),
    ]
    db_session.commit()

    # limit=3 guarantees multiple pages and forces at least one page break to
    # land inside a duplicate-key run and another across the null boundary.
    walked = _walk_all_pages(db_session, user, limit=3)

    walked_ids = [item["id"] for item in walked]
    expected_ids = [item.id for item in _expected_order(seeded)]
    assert walked_ids == expected_ids
    assert len(walked_ids) == len(set(walked_ids))  # no dup
    assert len(walked_ids) == len(seeded)  # no skip


def test_pagination_walk_starting_in_the_null_segment_continues_correctly(db_session):
    # All ten rows null-due -- exercises the "cursor minted in the null
    # segment continues correctly" edge case on its own, not just as the
    # tail of a mixed walk.
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    seeded = [
        _seed_action_item(db_session, thread, due_at=None, created_at=_t(-i))
        for i in range(10)
    ]
    db_session.commit()

    walked = _walk_all_pages(db_session, user, limit=4)

    assert [item["id"] for item in walked] == [item.id for item in _expected_order(seeded)]


def test_row_completed_between_pages_is_neither_reserved_nor_errors(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    seeded = [
        _seed_action_item(db_session, thread, due_at=None, created_at=_t(-i))
        for i in range(6)
    ]
    db_session.commit()

    page_one = actions.list_actions(
        status="open", limit=3, cursor=None, current_user=user, db=db_session
    )
    assert len(page_one["items"]) == 3
    assert page_one["next_cursor"] is not None

    returned_so_far = {item["id"] for item in page_one["items"]}
    not_yet_returned = [item for item in seeded if item.id not in returned_so_far]
    completed = not_yet_returned[0]
    completed.status = "done"
    db_session.commit()

    page_two = actions.list_actions(
        status="open",
        limit=3,
        cursor=page_one["next_cursor"],
        current_user=user,
        db=db_session,
    )
    page_two_ids = {item["id"] for item in page_two["items"]}
    assert completed.id not in page_two_ids
    assert completed.id not in returned_so_far
    # Nothing else got skipped or duplicated by the status flip -- the
    # combined pages account for every row except the one that left "open".
    all_ids = returned_so_far | page_two_ids
    assert all_ids == {item.id for item in seeded} - {completed.id}


def test_counts_unchanged_across_pages(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    for i in range(5):
        _seed_action_item(db_session, thread, due_at=None, created_at=_t(-i))
    db_session.commit()

    page_one = actions.list_actions(
        status="open", limit=3, cursor=None, current_user=user, db=db_session
    )
    page_two = actions.list_actions(
        status="open",
        limit=3,
        cursor=page_one["next_cursor"],
        current_user=user,
        db=db_session,
    )
    assert page_one["counts"] == page_two["counts"] == {"open": 5, "overdue": 0}
