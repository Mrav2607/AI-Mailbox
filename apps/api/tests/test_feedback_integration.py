"""Real-Postgres coverage for `classification_feedback` (plan
docs/plans/2026-08-11-feedback-capture-plan.md §3.1/§3.3/§3.4/§5).

Same idiom as test_auth_integration.py: opt-in via TEST_DATABASE_URL so the
offline suite stays green without a live database, schema built with
`Base.metadata.create_all` against a `*_test`-only database, one truncated
session per test. What this tier proves that no fake DB can: CHECK/FK
constraint enforcement, `ON CONFLICT ... WHERE` rowcount semantics (the
"written"/"protected" split), real row-lock concurrency across two sessions,
and whether the query planner can actually use
`ix_classification_feedback_msg_seq` for the export's ordering.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    AppUser,
    Classification,
    ClassificationFeedback,
    MailMessage,
    MailThread,
    ProviderAccount,
)
from app.services.nlp import feedback_export
from app.services.nlp.backfill import latest_message_ordering, latest_messages_by_thread
from app.services.nlp.persistence import OPERATOR_MODEL_VERSION, upsert_classification

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
    classification, and classification_feedback with it, so each test starts
    from empty without needing to know every downstream table by name.
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


def _seed_message(
    db, thread, *, sent_at=None, snippet="hi", body_text="there", created_at=None
):
    message = MailMessage(
        thread_id=thread.id,
        provider_message_id=str(uuid4()),
        sent_at=sent_at,
        snippet=snippet,
        body_text=body_text,
    )
    # An explicit created_at overrides the server default -- the tie-break
    # test below needs two rows with a genuinely identical timestamp, which
    # the default can no longer produce (clock_timestamp() since migration
    # 0023 stamps each insert individually).
    if created_at is not None:
        message.created_at = created_at
    db.add(message)
    db.flush()
    return message


def _seed_message_with_classification(
    db, thread, *, label="fyi", model_version="heuristic-v1", confidence=0.6
):
    message = _seed_message(db, thread)
    db.add(
        Classification(
            message_id=message.id,
            label=label,
            confidence=confidence,
            model_version=model_version,
        )
    )
    db.flush()
    return message


def _insert_feedback(
    db,
    *,
    user_id,
    message_id,
    new_label,
    prior_label=None,
    prior_confidence=None,
    prior_model_version=None,
    input_text="snapshot",
    source="reclassify",
):
    return db.execute(
        insert(ClassificationFeedback)
        .values(
            user_id=user_id,
            message_id=message_id,
            input_text=input_text,
            prior_label=prior_label,
            prior_confidence=prior_confidence,
            prior_model_version=prior_model_version,
            new_label=new_label,
            source=source,
        )
        .returning(ClassificationFeedback.id, ClassificationFeedback.capture_seq)
    ).one()


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


def test_ck_label_rejects_a_label_outside_the_frozen_taxonomy(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    message = _seed_message(db_session, thread)

    db_session.add(
        ClassificationFeedback(
            user_id=user.id,
            message_id=message.id,
            input_text="hi",
            new_label="not_a_real_label",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_ck_source_rejects_anything_but_reclassify(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    message = _seed_message(db_session, thread)

    db_session.add(
        ClassificationFeedback(
            user_id=user.id,
            message_id=message.id,
            input_text="hi",
            new_label="fyi",
            source="confirm",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


# ---------------------------------------------------------------------------
# ON DELETE CASCADE (D4, §3.5)
# ---------------------------------------------------------------------------


def test_cascade_deletes_feedback_for_two_deleted_messages(db_session):
    # Codex P1-1's exact concern: prove this for more than one row so a bug
    # that only clears the FIRST deleted message's feedback can't hide.
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    message_a = _seed_message(db_session, thread)
    message_b = _seed_message(db_session, thread)
    _insert_feedback(db_session, user_id=user.id, message_id=message_a.id, new_label="fyi")
    _insert_feedback(db_session, user_id=user.id, message_id=message_b.id, new_label="spam")
    db_session.commit()

    db_session.execute(
        text("DELETE FROM mail_message WHERE id IN (:a, :b)"),
        {"a": message_a.id, "b": message_b.id},
    )
    db_session.commit()

    remaining = db_session.execute(select(ClassificationFeedback)).scalars().all()
    assert remaining == []


def test_cascade_deletes_feedback_when_its_user_is_deleted(db_session):
    # Isolates the user_id FK specifically: the feedback SUBMITTER (userB)
    # gets deleted while the thread/message it corrected belongs to a
    # different account (userA) and must survive untouched.
    owner, owner_account = _seed_user_and_account(db_session, email="owner@example.com")
    submitter, _ = _seed_user_and_account(db_session, email="submitter@example.com")
    thread = _seed_thread(db_session, owner, owner_account)
    message = _seed_message(db_session, thread)
    _insert_feedback(db_session, user_id=submitter.id, message_id=message.id, new_label="fyi")
    db_session.commit()

    db_session.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": submitter.id})
    db_session.commit()

    remaining = db_session.execute(select(ClassificationFeedback)).scalars().all()
    assert remaining == []
    # The message this feedback pointed at is untouched -- only the
    # submitter's own row (via user_id) was ever supposed to go.
    assert db_session.get(MailMessage, message.id) is not None


# ---------------------------------------------------------------------------
# Append-only accumulation + latest-per-message export semantics
# ---------------------------------------------------------------------------


def test_append_only_history_keeps_every_event_export_picks_the_latest(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    message = _seed_message(db_session, thread)

    _insert_feedback(
        db_session, user_id=user.id, message_id=message.id, new_label="fyi",
        prior_label=None,
    )
    db_session.commit()
    _insert_feedback(
        db_session, user_id=user.id, message_id=message.id, new_label="promotional",
        prior_label="fyi", prior_model_version=OPERATOR_MODEL_VERSION,
    )
    db_session.commit()
    _insert_feedback(
        db_session, user_id=user.id, message_id=message.id, new_label="action_required",
        prior_label="promotional", prior_model_version=OPERATOR_MODEL_VERSION,
    )
    db_session.commit()

    # Append-only: nothing got overwritten, all three rows are still there.
    history = (
        db_session.execute(
            select(ClassificationFeedback)
            .where(ClassificationFeedback.message_id == message.id)
            .order_by(ClassificationFeedback.capture_seq)
        )
        .scalars()
        .all()
    )
    assert [row.new_label for row in history] == ["fyi", "promotional", "action_required"]

    # Export resolves to the LATEST event for this message, not the first or
    # a middle one.
    latest = list(feedback_export._latest_feedback_rows(db_session, user_id=None))
    assert len(latest) == 1
    assert latest[0].new_label == "action_required"
    assert latest[0].prior_label == "promotional"


def test_export_latest_per_message_across_multiple_messages(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    message_a = _seed_message(db_session, thread)
    message_b = _seed_message(db_session, thread)

    _insert_feedback(db_session, user_id=user.id, message_id=message_a.id, new_label="fyi")
    _insert_feedback(db_session, user_id=user.id, message_id=message_a.id, new_label="spam")
    _insert_feedback(db_session, user_id=user.id, message_id=message_b.id, new_label="needs_reply")
    db_session.commit()

    latest = {
        row.message_id: row.new_label
        for row in feedback_export._latest_feedback_rows(db_session, user_id=None)
    }
    assert latest == {message_a.id: "spam", message_b.id: "needs_reply"}


# ---------------------------------------------------------------------------
# Conditional upsert: "written" then "protected" (plan §3.3, Codex P2-2) --
# only real Postgres can prove the ON CONFLICT ... WHERE + rowcount split
# honestly; the fakes elsewhere just return whatever the test tells them to.
# ---------------------------------------------------------------------------


def test_conditional_upsert_reports_written_then_protected_against_an_override(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    message = _seed_message(db_session, thread)

    first = upsert_classification(
        db_session,
        message_id=message.id,
        label="fyi",
        confidence=0.5,
        rationale="r",
        model_version="heuristic-v1",
    )
    assert first == "written"
    db_session.commit()

    override = upsert_classification(
        db_session,
        message_id=message.id,
        label="action_required",
        confidence=1.0,
        rationale="operator override",
        model_version=OPERATOR_MODEL_VERSION,
        overwrite_user_override=True,
    )
    assert override == "written"
    db_session.commit()

    # A model path retrying after the override landed must be protected, not
    # silently clobber it.
    protected = upsert_classification(
        db_session,
        message_id=message.id,
        label="promotional",
        confidence=0.9,
        rationale="model retry",
        model_version="heuristic-v2",
    )
    assert protected == "protected"
    db_session.commit()

    row = (
        db_session.execute(select(Classification).where(Classification.message_id == message.id))
        .scalars()
        .one()
    )
    assert row.label == "action_required"
    assert row.model_version == OPERATOR_MODEL_VERSION


# ---------------------------------------------------------------------------
# Equal-timestamp latest-message tie-break (Codex P2-6): two messages with an
# identical sent_at (both NULL, falling back to created_at) and an EXPLICIT
# identical created_at. This used to lean on Postgres's now() being
# transaction-start time (same txn == same stamp), but created_at defaults to
# clock_timestamp() since migration 0023 -- per-insert stamps, so the tie has
# to be seeded explicitly now. The behavior under test is unchanged: on a
# true timestamp tie, the id decides, deterministically.
# ---------------------------------------------------------------------------


def test_equal_timestamp_latest_message_tie_breaks_on_message_id(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    tie_instant = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    message_a = _seed_message(db_session, thread, sent_at=None, created_at=tie_instant)
    message_b = _seed_message(db_session, thread, sent_at=None, created_at=tie_instant)
    db_session.commit()

    expected_winner = max(message_a.id, message_b.id)

    picked = latest_messages_by_thread(
        db_session, [thread.id], columns=(MailMessage.id, MailMessage.thread_id)
    )
    assert picked[thread.id].id == expected_winner

    # Deterministic across repeats -- not "whichever the planner happened to
    # return first out of a genuine tie".
    picked_again = latest_messages_by_thread(
        db_session, [thread.id], columns=(MailMessage.id, MailMessage.thread_id)
    )
    assert picked_again[thread.id].id == expected_winner


# ---------------------------------------------------------------------------
# Two-session lock barrier (Codex P2-7): txn A locks the thread's latest
# message FOR UPDATE, txn B blocks trying to acquire the same lock, A
# commits its override, B resumes -- B must have read A's committed override
# as ITS prior, and the export must select B's (later) event.
# ---------------------------------------------------------------------------


def _capture_hook(db, *, thread_id, user_id, label):
    """Mirrors reclassify_thread's capture hook (plan §3.2) using the same
    shared helpers the route uses -- lock, read prior under the lock, insert
    feedback, conditional upsert -- without the route's HTTP/validation
    plumbing, so each half of the barrier test can be paced independently.
    Does not commit; the caller controls the transaction boundary.
    """
    latest_message = (
        db.execute(
            select(MailMessage)
            .where(MailMessage.thread_id == thread_id)
            .order_by(*latest_message_ordering())
            .limit(1)
            .with_for_update()
        )
        .scalars()
        .first()
    )
    prior = (
        db.execute(select(Classification).where(Classification.message_id == latest_message.id))
        .scalars()
        .first()
    )
    db.execute(
        insert(ClassificationFeedback).values(
            user_id=user_id,
            message_id=latest_message.id,
            input_text="barrier test snapshot",
            prior_label=prior.label if prior else None,
            prior_confidence=prior.confidence if prior else None,
            prior_model_version=prior.model_version if prior else None,
            new_label=label,
        )
    )
    upsert_classification(
        db,
        message_id=latest_message.id,
        label=label,
        confidence=1.0,
        rationale="test override",
        model_version=OPERATOR_MODEL_VERSION,
        overwrite_user_override=True,
    )
    return latest_message.id


def test_two_session_lock_barrier_serializes_the_prior_read(_engine):
    session_factory = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    setup = session_factory()
    setup.execute(text("TRUNCATE app_user CASCADE"))
    setup.commit()
    user, account = _seed_user_and_account(setup)
    thread = _seed_thread(setup, user, account)
    message = _seed_message_with_classification(setup, thread, label="fyi", model_version="heuristic-v1")
    setup.commit()
    thread_id, user_id, message_id = thread.id, user.id, message.id
    setup.close()

    db_a = session_factory()
    db_b = session_factory()
    b_started = threading.Event()
    b_done = threading.Event()
    b_errors: list[Exception] = []

    def run_b():
        b_started.set()
        try:
            _capture_hook(db_b, thread_id=thread_id, user_id=user_id, label="action_required")
            db_b.commit()
        except Exception as exc:  # pragma: no cover - surfaced via b_errors
            db_b.rollback()
            b_errors.append(exc)
        finally:
            b_done.set()

    try:
        # A acquires the row lock first and holds its transaction open --
        # no commit yet.
        _capture_hook(db_a, thread_id=thread_id, user_id=user_id, label="promotional")

        b_thread = threading.Thread(target=run_b, daemon=True)
        b_thread.start()
        assert b_started.wait(timeout=5), "B's thread never started"
        # B should be blocked on A's row lock: if the lock weren't holding it
        # up, this trivial write would finish well within half a second.
        assert not b_done.wait(timeout=0.5), "B ran without waiting for A's lock"

        db_a.commit()
        assert b_done.wait(timeout=5), "B never resumed after A committed"
        b_thread.join(timeout=5)
    finally:
        db_a.close()
        db_b.close()

    if b_errors:
        raise b_errors[0]

    verify = session_factory()
    try:
        rows = (
            verify.execute(
                select(ClassificationFeedback)
                .where(ClassificationFeedback.message_id == message_id)
                .order_by(ClassificationFeedback.capture_seq)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        a_row, b_row = rows
        assert a_row.new_label == "promotional"
        assert a_row.prior_label == "fyi"
        # The whole point: B re-read the prior classification UNDER the
        # lock, so it sees A's committed override -- never the stale
        # pre-A model label.
        assert b_row.new_label == "action_required"
        assert b_row.prior_label == "promotional"
        assert b_row.prior_model_version == OPERATOR_MODEL_VERSION

        exported = list(feedback_export._latest_feedback_rows(verify, user_id=None))
        assert len(exported) == 1
        assert exported[0].capture_seq == b_row.capture_seq
        assert exported[0].new_label == "action_required"
    finally:
        verify.rollback()
        verify.close()


# ---------------------------------------------------------------------------
# Index proof (Codex P2-1/P3-4): ix_classification_feedback_msg_seq actually
# serves the export's ORDER BY, and is declared with the ordered columns the
# plan requires.
# ---------------------------------------------------------------------------


def test_index_serves_the_export_ordering_under_forced_index_scan(db_session):
    user, account = _seed_user_and_account(db_session)
    thread = _seed_thread(db_session, user, account)
    message_a = _seed_message(db_session, thread)
    message_b = _seed_message(db_session, thread)
    _insert_feedback(db_session, user_id=user.id, message_id=message_a.id, new_label="fyi")
    _insert_feedback(db_session, user_id=user.id, message_id=message_a.id, new_label="spam")
    _insert_feedback(db_session, user_id=user.id, message_id=message_b.id, new_label="needs_reply")
    db_session.commit()

    stmt = feedback_export._latest_feedback_statement(user_id=None)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    # A tiny fixture table is cheap enough that the planner would rightly
    # seq-scan it -- forcing the index off proves the index CAN serve this
    # query, which is the actual claim, not "the planner happened to pick it
    # here."
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan_rows = db_session.execute(text(f"EXPLAIN {compiled}")).all()
    plan_text = "\n".join(row[0] for row in plan_rows)

    assert "Seq Scan" not in plan_text
    assert "ix_classification_feedback_msg_seq" in plan_text


def test_index_declared_ordered_columns_match_the_plan(db_session):
    row = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_classification_feedback_msg_seq'"
        )
    ).first()

    assert row is not None, "ix_classification_feedback_msg_seq is missing from the schema"
    indexdef = row.indexdef.lower()
    # message_id ASC (no modifier), capture_seq DESC -- the exact mixed order
    # DISTINCT ON (message_id) ... ORDER BY message_id, capture_seq DESC
    # needs; a plain ASC/ASC btree can't produce it.
    assert "(message_id, capture_seq desc)" in indexdef
