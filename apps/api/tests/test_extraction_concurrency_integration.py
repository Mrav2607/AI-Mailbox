"""Real-Postgres coverage for the extraction retry-storm investigation
(plan: docs/plans/2026-08-19-extraction-cost-hardening-plan.md, D1/D2/D4).

Same idiom as test_llm_credentials_integration.py: opt-in via
TEST_DATABASE_URL, schema built with `Base.metadata.create_all` against a
`*_test`-only database, real row-lock (and, here, real DBAPI rowcount)
behavior proven against actual Postgres -- test_extraction_run.py's own
module docstring says exactly why this tier exists: no fake `_FakeDB` can
ever catch a real driver's rowcount-reporting quirk, because a fake's
rowcount is always whatever the test canned it to be. This file IS that
live QA, promoted to the CI-gated real-Postgres tier.

D1's actual finding, established here (NOT the two-connection race the
plan's own candidate-path framing led with -- that turned out to be a
symptom, not the mechanism): `claim_action_item`'s fresh-row INSERT
(`ON CONFLICT DO NOTHING`) never set `execution_options(preserve_rowcount=
True)`. Under psycopg3, an INSERT ... ON CONFLICT statement that takes the
DO-NOTHING path (the conflict fired, zero rows actually inserted) reports
`rowcount == -1` -- truthy -- without that execution option, exactly the
same trap `upsert_classification` already documents and guards against a
few functions up in this same module. `claim_action_item` treated any
truthy rowcount as "my insert won" and returned a claim_token, so EVERY
reclaim of an EXISTING action_item row -- no concurrency required at all,
proven single-threaded below -- silently failed: the INSERT always
conflicts (the row already exists), always reports a phantom "success", and
`claim_action_item` returns BEFORE it ever reaches the real UPDATE that
would have reclaimed the row and bumped `attempts`. The caller believes it
holds a valid claim, spends a real wire call, and then `record_extraction`'s
fenced UPDATE affects zero rows (the row's real `claim_token` is `None`,
its real `outcome` is `'failed'`, neither matches the phantom token) --
bucket `"skipped"`, with the attempt's genuine `failure_category` still
attached. That's the EXACT prod triple: `skipped` bucket + populated
`failure_categories` + `attempts` that never advance past their first-ever
value, because the write that would have advanced them never happened.

`test_reclaim_of_an_existing_failed_row_is_never_silently_dropped` proves
this directly, one session, zero threads. The remaining two tests show it
also governs what happens under REAL concurrency (two connections' sweeps
racing the same candidates, per the operator's setup) -- and that D2 bounds
whatever damage a single genuine failure can still do once D4's fix is in
place.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    ActionItem,
    AppUser,
    Classification,
    MailMessage,
    MailThread,
    ProviderAccount,
    UserLlmCredential,
)
from app.services.nlp import extraction_run, persistence
from app.services.nlp.extractor import ExtractionAttempt
from app.services.nlp.providers import CallContext, LlmCredential

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to run the real-Postgres integration tier",
)

_JOIN_TIMEOUT = 10.0
_EVENT_TIMEOUT = 5.0


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
def session_factory(_engine):
    """A fresh `sessionmaker` per test, with a clean `app_user` table.
    TRUNCATE ... CASCADE takes provider_account/mail_thread/mail_message/
    classification/action_item/user_llm_credential with it, so each test
    starts from empty. Every worker thread below opens its OWN session from
    this factory -- never sharing a session across threads.
    """
    factory = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    setup = factory()
    setup.execute(text("TRUNCATE app_user CASCADE"))
    setup.commit()
    setup.close()
    return factory


def _seed(factory, *, model_version="openai:gpt-4o-mini"):
    """One user, one active BYOK credential, one thread, one actionable
    message with no existing action_item row (a never-claimed candidate --
    the exact shape the prod storm's fresh-mail candidates start as).
    Returns (user_id, credential_id, credential_revision, message_id).
    """
    db = factory()
    try:
        user = AppUser(email=f"{uuid4()}@example.com")
        db.add(user)
        db.flush()

        credential = UserLlmCredential(
            user_id=user.id,
            name="default",
            is_active=True,
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-key-1234",
            model="gpt-4o-mini",
            revision=1,
        )
        db.add(credential)
        db.flush()

        account = ProviderAccount(
            user_id=user.id, provider="gmail", external_user_id="op@example.com",
            access_token="access",
        )
        db.add(account)
        db.flush()

        thread = MailThread(
            user_id=user.id, provider_account_id=account.id, provider="gmail",
            provider_thread_id=str(uuid4()), subject="Invoice due",
        )
        db.add(thread)
        db.flush()

        message = MailMessage(
            thread_id=thread.id, provider_message_id=str(uuid4()),
            snippet="hi", body_text="pay by friday",
        )
        db.add(message)
        db.flush()
        db.add(
            Classification(
                message_id=message.id, label="needs_reply", confidence=0.9,
                model_version="test",
            )
        )
        db.commit()
        return user.id, credential.id, credential.revision, message.id
    finally:
        db.close()


def _call_context(credential_id, revision, *, model="gpt-4o-mini") -> CallContext:
    return CallContext(
        credential=LlmCredential(
            provider="openai", base_url="https://api.openai.com/v1",
            api_key="sk-test-key-1234", model=model,
        ),
        payer="user",
        credential_id=credential_id,
        revision=revision,
    )


def _row_state(factory, message_id):
    db = factory()
    try:
        row = db.query(ActionItem).filter(ActionItem.message_id == message_id).one()
        return {
            "outcome": row.outcome,
            "attempts": row.attempts,
            "claim_token": row.claim_token,
        }
    finally:
        db.close()


def test_reclaim_of_an_existing_failed_row_is_never_silently_dropped(session_factory):
    """D1's cornerstone repro -- and it needs no concurrency at all.

    One session, sequential, no threads: claim a brand-new row (a genuine
    INSERT), settle it to `failed` (a normal 404), then claim it again --
    the ordinary "next sweep retries a failed row" path every extraction
    sweep relies on. Before the D4 fix (missing `preserve_rowcount=True`
    on `claim_action_item`'s INSERT), the second `claim_action_item` call
    returned a real-looking `claim_token` while the row underneath was
    COMPLETELY UNTOUCHED -- still `outcome='failed'`, `attempts=1`,
    `claim_token=None`. Confirmed directly against this DB before the fix
    landed (git-stashed persistence.py, same script this test encodes): a
    second claim's `rowcount` on the conflicting INSERT came back truthy
    (psycopg3's -1-for-ON-CONFLICT-DO-NOTHING quirk), so `claim_action_item`
    returned early and never reached the UPDATE that would have actually
    reclaimed the row.

    With the fix, this claim genuinely reclaims: `outcome='pending'`,
    `attempts=2`, `claim_token` matching what was just returned. That's the
    whole D1/D4 story -- every retry of every failed extraction row was
    silently a no-op, which is exactly "the attempts-cap machinery... is
    demonstrably NOT terminalizing these" from the plan's own prod evidence.
    """
    user_id, credential_id, revision, message_id = _seed(session_factory)

    db = session_factory()
    try:
        token1 = persistence.claim_action_item(
            db, message_id=message_id, thread_id=db.get(MailMessage, message_id).thread_id,
            user_id=user_id, thread_done=False,
        )
        db.commit()
    finally:
        db.close()

    db = session_factory()
    try:
        recorded = persistence.record_extraction(
            db, message_id=message_id, claim_token=token1, result=None,
            thread_done=False, label_still_actionable=True,
        )
        db.commit()
    finally:
        db.close()
    assert recorded is True

    state = _row_state(session_factory, message_id)
    assert state == {"outcome": "failed", "attempts": 1, "claim_token": None}

    # The retry every sweep after this one relies on: reclaim the same,
    # now-`failed`, row.
    db = session_factory()
    try:
        token2 = persistence.claim_action_item(
            db, message_id=message_id, thread_id=db.get(MailMessage, message_id).thread_id,
            user_id=user_id, thread_done=False,
        )
        db.commit()
    finally:
        db.close()

    assert token2 is not None
    assert token2 != token1

    state = _row_state(session_factory, message_id)
    # This is the assertion that fails without the D4 fix: state stays
    # {"failed", 1, None} -- claim_action_item lied about reclaiming it.
    assert state == {"outcome": "pending", "attempts": 2, "claim_token": token2}


def test_two_concurrent_claims_on_a_brand_new_row_self_limits(monkeypatch, session_factory):
    """The concurrent variant of the SAME D4 bug: two REAL sessions, in two
    REAL threads, racing `_claim_extract_record` on the SAME never-claimed
    message at essentially the same instant (a `threading.Barrier`, not a
    scripted pause). A genuine simultaneous INSERT race is a real Postgres
    conflict too -- the loser's insert blocks until the winner commits, then
    resolves as a conflict -- so this is the SAME missing-`preserve_rowcount`
    defect, just triggered by two connections instead of one session's
    retry. Without the fix, the loser's conflicting INSERT ALSO reports a
    phantom truthy rowcount and returns a bogus token, wasting a real wire
    call and fencing out at record time -- bucket "skipped" with a real
    category, never "skipped" with `None`. With the fix, the loser's insert
    correctly resolves to a genuine claim failure BEFORE any wire call --
    bucket "skipped", category `None`, exactly matching what Codex's
    original source trace predicted for a clean race (plan changelog v2).
    """
    user_id, credential_id, revision, message_id = _seed(session_factory)

    def fake_extract(*, credential, **kwargs):
        return ExtractionAttempt(
            result=None, provider_call_succeeded=True, usage=None,
            llm_attempted=True, fallback_used=False, failure_category="http_404",
        )

    monkeypatch.setattr(extraction_run, "extract_action_with_usage", fake_extract)

    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def worker(name):
        db = session_factory()
        try:
            barrier.wait(timeout=_EVENT_TIMEOUT)
            categories: dict = {}
            bucket, uid, category = extraction_run._claim_extract_record(
                db, message_id,
                call_context=_call_context(credential_id, revision),
                failure_categories=categories,
            )
            results[name] = (bucket, category)
        except BaseException as exc:
            errors[name] = exc
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT)

    # Catch a hung/still-running thread and a worker exception BEFORE
    # touching `results` -- a bare KeyError from a thread that silently
    # died is a much worse failure message than surfacing what it raised.
    for t in threads:
        assert not t.is_alive(), f"worker thread {t.name} did not finish in time"
    assert not errors, f"worker thread(s) raised: {errors}"

    outcomes = [results["A"], results["B"]]
    winners = [r for r in outcomes if r[0] == "failed"]
    losers = [r for r in outcomes if r[0] == "skipped"]

    assert len(winners) == 1, f"expected exactly one winner, got {outcomes}"
    assert len(losers) == 1, f"expected exactly one loser, got {outcomes}"
    assert winners[0][1] == "http_404"  # the winner's own genuine attempt
    assert losers[0][1] is None  # the loser never reached the wire at all

    # And the row itself: one genuine attempt, D2's cap-jump already applied
    # (a first-ever claim's credential-class failure caps straight to
    # MAX_ATTEMPTS since there's nothing to have raced a rotation against).
    state = _row_state(session_factory, message_id)
    assert state["outcome"] == "failed"
    assert state["attempts"] == persistence.MAX_ATTEMPTS


def test_stale_lease_competing_reclaim_bounded_by_d2(monkeypatch, session_factory):
    """A SEPARATE, secondary race from the D4 bug above -- included because
    the plan's own D1 candidate list named it, and because it's a genuine
    interleaving worth bounding regardless of D4: thread A claims the row
    (a genuine first-ever claim, attempts=1), then blocks INSIDE its own
    "wire call" (a stubbed `extract_action_with_usage`, gated on a real
    `threading.Event` -- not a scripted mock). While A is blocked, the test
    backdates the row's `last_attempted_at` past `PENDING_LEASE` directly in
    Postgres, simulating exactly what an orphaned/crashed worker's claim
    looks like after 30 minutes: a live claim nobody is coming back to
    finish. Thread B then reclaims it for real (Postgres's own claimable_
    predicate, not a stub) -- a completely normal expired-pending reclaim,
    attempts -> 2 -- and completes its OWN failed attempt, recording
    normally. Only THEN is A released to finish its stale attempt and try to
    record with its now-superseded claim_token.

    This ALSO produces the skipped-bucket-with-a-real-category combination
    on A's own return, but via a genuinely different mechanism than D4's bug
    (a truly expired lease, not a misreported rowcount) -- it needs an
    already-orphaned claim to exist first (a crashed/killed worker), which
    is a MUCH rarer precondition than "a second reclaim of any failed row,"
    the D4 bug's trigger. This test's real job is proving D2 bounds it
    either way: B's own record (a genuine credential-class failure, same
    active credential the whole time -- no rotation) cap-jumps the row
    straight to MAX_ATTEMPTS. The row is terminal after this ONE extra
    wasted call (B's), not a storm that repeats every 30 minutes.
    """
    user_id, credential_id, revision, message_id = _seed(session_factory)

    release_a = threading.Event()
    a_blocked = threading.Event()

    def fake_extract(*, credential, **kwargs):
        if threading.current_thread().name == "A":
            a_blocked.set()
            if not release_a.wait(timeout=_EVENT_TIMEOUT):
                raise AssertionError("main thread never released A")
        return ExtractionAttempt(
            result=None, provider_call_succeeded=True, usage=None,
            llm_attempted=True, fallback_used=False, failure_category="http_404",
        )

    monkeypatch.setattr(extraction_run, "extract_action_with_usage", fake_extract)

    results = {}
    errors = {}

    def run_a():
        db = session_factory()
        try:
            bucket, uid, category = extraction_run._claim_extract_record(
                db, message_id,
                call_context=_call_context(credential_id, revision),
                failure_categories={},
            )
            results["A"] = (bucket, category)
        except BaseException as exc:
            errors["A"] = exc
        finally:
            db.close()

    thread_a = threading.Thread(target=run_a, name="A")
    thread_a.start()
    assert a_blocked.wait(timeout=_EVENT_TIMEOUT), "A never reached its wire call"

    # A's claim has committed (the claim transaction closes before the wire
    # call runs) and is sitting there genuinely 'pending' -- back-date it
    # past the lease, exactly like a claim a crashed worker never returned
    # to finish would look after 30 real minutes.
    backdate_db = session_factory()
    try:
        backdate_db.execute(
            update(ActionItem)
            .where(ActionItem.message_id == message_id)
            .values(
                last_attempted_at=datetime.now(timezone.utc)
                - persistence.PENDING_LEASE
                - timedelta(minutes=1)
            )
        )
        backdate_db.commit()
    finally:
        backdate_db.close()

    # B reclaims for real -- a completely ordinary expired-pending reclaim,
    # no stubbing of the claim/record path at all.
    db_b = session_factory()
    try:
        bucket_b, uid_b, category_b = extraction_run._claim_extract_record(
            db_b, message_id,
            call_context=_call_context(credential_id, revision),
            failure_categories={},
        )
    finally:
        db_b.close()

    # Now let A's stale attempt finish and try to record.
    release_a.set()
    thread_a.join(timeout=_JOIN_TIMEOUT)

    assert not thread_a.is_alive(), "thread A did not finish in time"
    assert not errors, f"worker thread(s) raised: {errors}"

    # B: a normal, successful record of a genuine failure.
    assert bucket_b == "failed"
    assert category_b == "http_404"

    # A: the D1 triple -- skipped bucket, real category, fenced out by B's
    # reclaim.
    assert "A" in results, "thread A never completed"
    bucket_a, category_a = results["A"]
    assert bucket_a == "skipped"
    assert category_a == "http_404"

    # The row itself: D2's cap-jump already terminalized it off B's write --
    # bounded to the one extra call B just spent, not a repeating storm.
    state = _row_state(session_factory, message_id)
    assert state["outcome"] == "failed"
    assert state["attempts"] == persistence.MAX_ATTEMPTS
    assert state["claim_token"] is None
