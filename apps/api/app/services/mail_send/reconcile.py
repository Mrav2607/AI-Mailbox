"""Narrow, correlated reply-attempt reconciliation (plan §3.5).

Level-triggered only (P7-1/P9-1): this module never runs inside an ingest
page/batch transaction -- those commit additions, removals, and cursors
atomically by design, and an independent per-attempt commit/rollback inside
them would corrupt that atomicity. Instead ``run_reconciliation_pass`` is
called at ingest START (before any provider traversal) and END (after the
run's final commit) by ``gmail_ingest.py``/``outlook_ingest.py``, and again
by the ``reconcile_reply_attempt`` Celery task for timeliness on an
``unknown``/Outlook-``sent``-unverified outcome.

Each matched attempt is reconciled as an ISOLATED unit in its own
transaction(s): settle `sent` + fence + action resolution FIRST (a provably
delivered attempt must never stay blocking because classification later
fails), then classify-if-claimed second. One permanently failing
classification can never block mailbox ingestion, other attempts, or the
reply guard (REVIEW.md's per-item-isolation rule).
"""

from __future__ import annotations

from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.models import Classification, MailMessage, MailThread, ReplyAttempt
from app.services.mail_send.common import (
    advance_fence_and_resolve,
    header_value,
    mark_sent_reconciled,
    stamp_verified,
)
from app.services.nlp.classification_breaker import ClassificationBreaker
from app.services.nlp.classifier import build_classification_text, classify_with_usage
from app.services.nlp.llm_client import WORKER_RETRIES
from app.services.nlp.persistence import upsert_classification
from app.services.nlp.providers import ClassificationRouter
from app.services.nlp.usage import UsageAccumulator

# Every status the partial index (`ix_reply_attempt_reconcile`) covers --
# kept identical to the index predicate on purpose, so "the index matches
# every reconciliation-eligible row and nothing else" is a real invariant,
# not just a comment (plan §3.5, P6-5).
_BLOCKING_STATUSES = ("preparing", "inflight", "unknown", "abandoned")

# `_classify_and_stamp`'s outcome kinds (Codex review, phase 2 correction):
# named constants rather than bare strings so a typo can't silently produce
# an outcome nobody's comparison recognizes. `_OUTCOME_NO_VERDICT` and
# `_OUTCOME_FAILED` both leave `classified=False` at the call site but are
# DELIBERATELY distinct -- a no-verdict pass genuinely completed (stamped,
# just nothing to classify with), so it's the only one of the five that
# should ever count toward `left_unclassified`. An exception is an unknown
# state, not a known "left unclassified" outcome, and `_OUTCOME_RACE_LOST`
# didn't determine the message's status at all (someone else already did).
#
# `_OUTCOME_NO_VERDICT` also covers one SKIP case (Codex review, phase 3):
# the run's `ClassificationBreaker` already tripped -- a message skipped that
# way was never counted anywhere else, so it still counts here. The OTHER
# skip case (this run already attempted this exact message) has its own
# non-counting outcome below.
_OUTCOME_CLASSIFIED = "classified"
_OUTCOME_ALREADY_CLASSIFIED = "already_classified"
_OUTCOME_NO_VERDICT = "no_verdict"
_OUTCOME_FAILED = "failed"
_OUTCOME_RACE_LOST = "race_lost"
# The message was already COUNTED as left-unclassified earlier in this SAME
# run -- by a real no-verdict attempt or a counted breaker-skip, either of
# which feeds the run-shared marker set -- so this outcome stamps
# verified_at without a second wire call AND without a second count (final
# Codex pass: reporting it as _OUTCOME_NO_VERDICT here made one unclassified
# Outlook sent message read as "2 left unclassified").
_OUTCOME_ALREADY_ATTEMPTED = "already_attempted"


def _rollback_quietly(db: Session, attempt_id: UUID) -> None:
    """Best-effort rollback for the accounting-preserving failure branches in
    `_classify_and_stamp` -- a dead connection makes rollback itself raise,
    and these branches must still return their (already-decided) outcome."""
    try:
        db.rollback()
    except Exception:
        logger.exception("rollback failed for attempt %s", attempt_id)


def _eligible_attempt_ids(
    db: Session, *, provider_account_id: UUID, provider: str
) -> list[UUID]:
    """Eligible attempts for ONE account, joined through
    `MailThread.provider_account_id` -- provider ids are mailbox-scoped, so
    a user's second account must never match the first account's attempts
    (P5-3)."""
    rows = (
        db.execute(
            select(ReplyAttempt.id)
            .join(MailThread, MailThread.id == ReplyAttempt.thread_id)
            .where(
                MailThread.provider_account_id == provider_account_id,
                ReplyAttempt.provider == provider,
                or_(
                    ReplyAttempt.status.in_(_BLOCKING_STATUSES),
                    and_(
                        ReplyAttempt.provider == "outlook",
                        ReplyAttempt.status == "sent",
                        ReplyAttempt.verified_at.is_(None),
                    ),
                ),
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _match_message(
    db: Session, *, attempt: ReplyAttempt, thread_id: UUID
) -> MailMessage | None:
    """Match ONE attempt against already-persisted messages on its own
    thread only. Gmail: case-insensitively located `Message-ID` header,
    whitespace-trimmed exact value comparison (the stored header map
    preserves provider casing), OR provider id equality. Outlook: provider
    id equals the recorded draft immutable id (valid across the draft ->
    Sent Items move)."""
    if attempt.provider == "outlook":
        if not attempt.provider_message_id:
            return None
        return (
            db.execute(
                select(MailMessage).where(
                    MailMessage.thread_id == thread_id,
                    MailMessage.provider_message_id == attempt.provider_message_id,
                )
            )
            .scalars()
            .first()
        )

    # Gmail
    target_header = (attempt.gmail_message_id_header or "").strip()
    candidates = db.execute(
        select(MailMessage).where(MailMessage.thread_id == thread_id)
    ).scalars().all()
    for message in candidates:
        if attempt.provider_message_id and message.provider_message_id == attempt.provider_message_id:
            return message
        if not target_header:
            continue
        for key, value in (message.headers or {}).items():
            if (
                isinstance(key, str)
                and key.lower() == "message-id"
                and isinstance(value, str)
                and value.strip() == target_header
            ):
                return message
    return None


def _settle_match(
    db: Session, *, attempt: ReplyAttempt, thread_id: UUID, message: MailMessage
) -> dict | None:
    """Phase 1, its own transaction: settle `sent` + fence + action
    resolution. Runs first and unconditionally on a match, regardless of
    whether classification later succeeds.

    R-1 (final review): the eligible set includes `unknown` attempts --
    settling those from the provider's own sent copy is this module's whole
    purpose -- so this uses `mark_sent_reconciled` (CAS from
    inflight/abandoned/unknown), not the send route's narrower `mark_sent`.
    A failed CAS is followed by a re-read, under the same thread lock, of
    the attempt's actual status: anything other than an already-`sent` row
    is a genuine anomaly (not a race this function knows how to resolve),
    so the fence/resolution below must NOT run -- returns `None` instead of
    silently advancing state for an attempt that isn't actually settled.
    """
    db.execute(select(MailThread).where(MailThread.id == thread_id).with_for_update())
    if attempt.status != "sent":
        settled = mark_sent_reconciled(
            db, attempt_id=attempt.id, provider_message_id=message.provider_message_id
        )
        if not settled:
            current_status = db.execute(
                select(ReplyAttempt.status).where(ReplyAttempt.id == attempt.id)
            ).scalar_one()
            if current_status != "sent":
                db.rollback()
                logger.warning(
                    "reply reconciliation: attempt %s matched a message but is in "
                    "unexpected status %r -- not settling",
                    attempt.id,
                    current_status,
                )
                return None
    resolved = advance_fence_and_resolve(
        db, thread_id=thread_id, attempt_created_at=attempt.created_at
    )
    db.commit()
    return {"resolved_action_items": resolved}


def _classify_and_stamp(
    db: Session,
    *,
    attempt_id: UUID,
    message: MailMessage,
    thread_id: UUID,
    user_id: UUID,
    breaker: ClassificationBreaker | None = None,
    # Run-shared MUTABLE set of provider_message_ids already attempted (and
    # left no-verdict) in this ingest run -- read to skip duplicate wire
    # calls AND fed by this module's own no-verdict outcomes, so a
    # START-pass failure protects the page loop and the END pass alike
    # (final Codex pass). None = no ingest-run context (the standalone
    # reconcile_reply_attempt task).
    left_unclassified_ids: set[str] | None = None,
) -> str:
    """Phase 2, ITS OWN transaction, isolated from phase 1: the
    classification claim (P8-2). Takes the shared serialization point
    (`MailThread` -> `ReplyAttempt` lock order, the same one used by every
    reconciliation path), re-reads `verified_at`, and only the null-winner
    classifies and stamps -- guaranteeing exactly one classifier invocation
    even when two workers race this same attempt.

    Returns one of the `_OUTCOME_*` constants (Codex review: the caller
    needs to tell `no_verdict` and `failed` apart to report
    `left_unclassified` correctly, and a bare bool couldn't say which of
    five different states produced it) -- see those constants' own comment
    for what each one means and why they're kept distinct.

    `verified_at` and "classification succeeded" are DELIBERATELY
    independent facts (Codex review, phase 2 correction): `verified_at`
    means the authoritative provider copy was confirmed (`reply_attempt.py`'s
    own docstring) -- for Outlook, that's THIS reconciliation pass finding
    the sentitems row, which genuinely happened whether or not classification
    did. `_settle_match` already stamps/settles unconditionally on a match,
    regardless of whether classification later succeeds -- this is the same
    principle applied to the classification phase's own outcomes:

    - `verdict is None` (D-C: a BYOK failure with no local fallback opted
      into): the send is still genuinely verified, so `stamp_verified` runs
      and commits same as a normal success -- just with no Classification
      row (`_OUTCOME_NO_VERDICT`). Leaving `verified_at` NULL here would
      make this attempt eligible forever (`_eligible_attempt_ids`'s Outlook
      `verified_at IS NULL` predicate), and with no row ever written to trip
      the duplicate-call guard above, EVERY future sync's reconciliation
      pass would re-issue the same already-known-to-fail BYOK call -- an
      unbounded retry loop that keeps billing the user and directly
      violates D-I (unclassified mail waits for a MANUAL backfill, nothing
      retries it automatically).
    - A genuine exception below (`except Exception`, `_OUTCOME_FAILED`) is a
      DIFFERENT, unknown state -- not a clean "the call ran and produced
      nothing" like the `verdict is None` case above, but "something broke
      and we don't know what state this left things in". Retrying a
      genuinely-unknown failure is defensible, so it still leaves
      `verified_at` NULL for a later pass; it never touches phase 1's
      already-committed settlement. These two paths look similar but are a
      DELIBERATE divergence, not an oversight -- do not "simplify" them
      back to matching each other, and do not count `_OUTCOME_FAILED`
      toward `left_unclassified` (it isn't a KNOWN "left unclassified"
      outcome the way `_OUTCOME_NO_VERDICT` is).

    Same-sync double call (Codex review, phase 2 -> fixed in phase 3): a
    `classify_messages=True` sync's own page loop can already have made one
    failed BYOK call for this message before the END reconciliation pass
    reaches here -- with no Classification row written (D-C), the
    duplicate-call guard above can't see that first attempt. Phase 2
    accepted this as "bounded at 2 calls, ever" -- with `WORKER_RETRIES`
    that is now up to 8 wire requests and ~120s of waits for one message,
    which no longer holds (Codex review, phase 3). `already_attempted_
    provider_message_ids` closes it: Outlook's page loop (the only ingest
    path that can classify a message which also correlates to a
    `ReplyAttempt` -- Gmail's send-completion path stamps `verified_at`
    immediately, so a Gmail attempt is never eligible here in the first
    place, see `reply.py`'s `_complete_gmail_send`) collects every
    `provider_message_id` it got a no-verdict outcome for in THIS run and
    hands the set to the END pass. A hit here stamps `verified_at` (same
    billing-loop-fix reasoning as the `verdict is None` branch below -- the
    attempt must not stay eligible forever) and reports `_OUTCOME_NO_VERDICT`
    WITHOUT a second wire call. The `Classification` row stays absent either
    way, so a manual backfill still finds the message.

    `breaker` (Codex review, phase 3 blocker) is this ingest run's shared
    `ClassificationBreaker` -- checked BEFORE the marker set, same
    stamp-and-report-no-verdict treatment, no wire call. Once tripped
    (3 consecutive no-verdict BYOK outcomes anywhere in this run) every
    later phase of the SAME run must stop issuing fresh calls into a
    provider already known to be failing, rather than this pass starting
    its own retry chain independently. `None` (the default) means no
    breaker context -- the standalone `reconcile_reply_attempt` Celery task
    calls this module with no ingest run backing it, so there's nothing to
    share.
    """
    db.execute(select(MailThread).where(MailThread.id == thread_id).with_for_update())
    current_verified_at = db.execute(
        select(ReplyAttempt.verified_at).where(ReplyAttempt.id == attempt_id).with_for_update()
    ).scalar_one_or_none()
    if current_verified_at is not None:
        # Someone else already won this race (or a prior pass already
        # verified it) -- nothing to do, and this call didn't determine the
        # message's classification status, so it's neither "classified" nor
        # "left unclassified" -- just a no-op.
        db.rollback()
        return _OUTCOME_RACE_LOST

    # R-3 (final review): a classification-enabled sync classifies the
    # persisted Sent Items message in its OWN page loop; the claim above
    # only guards against a second reconciliation worker via verified_at, so
    # without this check the END pass classifies the SAME message again --
    # a double BYOK charge, and a real risk of overwriting an already-good
    # verdict (or a user override). If a Classification row already exists
    # (model-driven or user-override, either way), this claim just stamps
    # verified_at and never calls the classifier or writes over it.
    existing_classification = db.execute(
        select(Classification.id).where(Classification.message_id == message.id)
    ).scalar_one_or_none()
    if existing_classification is not None:
        stamp_verified(db, attempt_id=attempt_id)
        db.commit()
        return _OUTCOME_ALREADY_CLASSIFIED

    # Final Codex pass: the already-attempted check runs BEFORE the breaker
    # check -- a message in the set was already counted into left_unclassified
    # at its first attempt, and the breaker branch below reports a countable
    # no-verdict, so the old order double-counted it whenever the breaker had
    # tripped by the time reconciliation got here. This run already made this
    # exact attempt and got no verdict (see this function's docstring's
    # "Same-sync double call" section) -- same stamp treatment, no second wire
    # call, and no second count.
    if (
        left_unclassified_ids is not None
        and message.provider_message_id in left_unclassified_ids
    ):
        # Best-effort stamp (verify pass): if it fails, the attempt stays
        # eligible and the NEXT encounter -- this run or the next -- hits
        # this same branch and tries again; the outcome is non-counting
        # either way, since the marker means this message was already
        # counted at its first attempt.
        try:
            stamp_verified(db, attempt_id=attempt_id)
            db.commit()
        except SoftTimeLimitExceeded:
            _rollback_quietly(db, attempt_id)
            raise
        except Exception:
            _rollback_quietly(db, attempt_id)
            logger.exception(
                "already-attempted stamp failed for attempt %s", attempt_id
            )
        return _OUTCOME_ALREADY_ATTEMPTED

    # Phase 3 (Codex review, blocker): the run's breaker already tripped --
    # do NOT start a fresh WORKER_RETRIES chain against a provider already
    # known to be failing this run. Stamp verified for the same reason the
    # `verdict is None` branch below does (an unverified attempt would stay
    # eligible forever), never write a Classification row, report it the
    # same way a real no-verdict call would.
    if breaker is not None and not breaker.should_call:
        # Mark it (verify pass): this outcome COUNTS into left_unclassified,
        # so without the marker a later phase of this run -- the page loop,
        # or a marker-less second encounter here -- would count the same
        # message again.
        if left_unclassified_ids is not None and message.provider_message_id:
            left_unclassified_ids.add(message.provider_message_id)
        try:
            stamp_verified(db, attempt_id=attempt_id)
            db.commit()
        except SoftTimeLimitExceeded:
            _rollback_quietly(db, attempt_id)
            raise
        except Exception:
            _rollback_quietly(db, attempt_id)
            logger.exception("breaker-skip stamp failed for attempt %s", attempt_id)
        return _OUTCOME_NO_VERDICT

    try:
        # R-7 (final review): the Subject header, read case-insensitively
        # from the stored `headers` JSONB the same way ingest does -- a
        # `None` subject here would classify this message differently than
        # the identical message classified through the normal ingest path,
        # violating the same-pipeline rule.
        subject = header_value(message.headers, "Subject")
        text_for_classification = build_classification_text(
            subject, message.snippet, message.body_text
        )
        router = ClassificationRouter(user_id)
        routing = router.routing_for(db)
        # This module only ever runs from ingest (worker) or the
        # reconcile_reply_attempt Celery task -- never inline off a request
        # (see this module's own docstring) -- so WORKER_RETRIES applies
        # unconditionally, not threaded from a caller.
        attempt_result = classify_with_usage(
            text_for_classification, routing=routing, policy=WORKER_RETRIES
        )
        # Run-shared accounting FIRST, before anything that can fail in the
        # DB (verify pass): once classify_with_usage returns, the wire
        # attempt is a fact -- a usage-flush or stamp failure below must
        # not erase it, or the streak misses a strike and a later phase
        # re-issues a paid call for the same message.
        no_verdict = attempt_result.verdict is None
        if breaker is not None:
            breaker.record(verdict_produced=not no_verdict)
        if (
            no_verdict
            and left_unclassified_ids is not None
            and message.provider_message_id
        ):
            left_unclassified_ids.add(message.provider_message_id)
        if routing.mode == "user" and routing.credential is not None:
            try:
                # SAVEPOINT, not a plain try/rollback (verify pass 3): a
                # full rollback here would release the MailThread/attempt
                # locks taken at the top of this function and then keep
                # writing OUTSIDE the serialization fence -- a concurrent
                # reconciliation could take the locks, see no verified_at,
                # and issue a second paid call. A failed savepoint rolls
                # back only the usage writes; the outer transaction and its
                # locks stay held.
                with db.begin_nested():
                    acc = UsageAccumulator(user_id)
                    acc.record(
                        "classification",
                        routing.credential.provider,
                        attempt_result.usage,
                        provider_call_succeeded=attempt_result.provider_call_succeeded,
                    )
                    acc.flush(db)
            except SoftTimeLimitExceeded:
                _rollback_quietly(db, attempt_id)
                raise
            except Exception:
                # Usage is telemetry -- losing one row beats returning
                # FAILED here, which would erase this attempt's countable
                # outcome and (marker set above) undercount the toast
                # (verify pass). If the CONNECTION itself died, the next
                # statement below fails too and the generic handler takes
                # over -- that residual (marker set, FAILED returned, one
                # toast unit undercounted) is the honest "unknown state"
                # case, not this branch's.
                logger.exception(
                    "usage flush failed during reply reconciliation for attempt %s",
                    attempt_id,
                )
        # Phase 2 correction (Codex review, D-C/D-I): `verdict is None` means
        # a failed BYOK call with no local fallback served -- but the SEND
        # itself is still genuinely verified (see this function's own
        # docstring for why that's a separate fact from classification
        # succeeding), so stamp_verified runs and commits here same as a
        # normal success. Skipping the stamp was the bug Codex caught: with
        # no Classification row ever written to trip the duplicate-call
        # guard above, and this attempt staying eligible forever, every
        # future sync's reconciliation pass would re-issue the same
        # already-known-to-fail BYOK call forever. Never write a null-label
        # row regardless (it would strand the message); usage above still
        # commits either way -- the call may have genuinely billed the user
        # even though it produced nothing to classify with.
        if no_verdict:
            # Breaker + marker were already fed above. Stamp best-effort:
            # a failure here still returns the countable NO_VERDICT (the
            # attempt stays eligible; the marker branch above re-stamps on
            # the next encounter).
            try:
                stamp_verified(db, attempt_id=attempt_id)
                db.commit()
            except SoftTimeLimitExceeded:
                _rollback_quietly(db, attempt_id)
                raise
            except Exception:
                _rollback_quietly(db, attempt_id)
                logger.exception("no-verdict stamp failed for attempt %s", attempt_id)
            return _OUTCOME_NO_VERDICT
        label, confidence, rationale, model_version = attempt_result.verdict
        upsert_classification(
            db,
            message_id=message.id,
            label=label,
            confidence=confidence,
            rationale=rationale,
            model_version=model_version,
        )
        stamp_verified(db, attempt_id=attempt_id)
        db.commit()
        return _OUTCOME_CLASSIFIED
    except SoftTimeLimitExceeded:
        # Celery's soft-limit signal is a plain Exception, so the generic
        # handler below WOULD swallow it and report an ordinary failure --
        # and the pass loop would keep issuing paid calls until the hard
        # kill (final Codex pass; same fix extraction_run.py already
        # carries). Roll back best-effort and let the signal propagate.
        try:
            db.rollback()
        except Exception:
            logger.exception(
                "rollback failed during soft-limit handling for attempt %s", attempt_id
            )
        raise
    except Exception:
        db.rollback()
        logger.exception(
            "reply reconciliation: classification failed for attempt %s (sent but unverified)",
            attempt_id,
        )
        # Deliberately never touches `breaker` -- this is an unknown/
        # containment failure (see this function's own docstring), not a
        # genuine LLM result, so it must not feed the same streak a real
        # no-verdict BYOK outcome does (same reasoning extraction_run.py's
        # _CONSECUTIVE_FAILURE_LIMIT already applies: a DB hiccup here would
        # otherwise misdiagnose as "the provider is failing").
        return _OUTCOME_FAILED


def _reconcile_one_attempt(
    db: Session,
    *,
    attempt_id: UUID,
    classify_messages: bool,
    breaker: ClassificationBreaker | None = None,
    # Run-shared MUTABLE set of provider_message_ids already attempted (and
    # left no-verdict) in this ingest run -- read to skip duplicate wire
    # calls AND fed by this module's own no-verdict outcomes, so a
    # START-pass failure protects the page loop and the END pass alike
    # (final Codex pass). None = no ingest-run context (the standalone
    # reconcile_reply_attempt task).
    left_unclassified_ids: set[str] | None = None,
) -> dict | None:
    """One isolated reconciliation unit. Returns `None` if there's nothing
    to do yet (no persisted message matches), else a summary dict.

    `breaker` and `left_unclassified_ids` pass straight
    through to `_classify_and_stamp` -- see its docstring for what each one
    prevents (phase 3 of the LLM-failure work, Codex review)."""
    attempt = db.get(ReplyAttempt, attempt_id)
    if attempt is None:
        return None

    thread = db.get(MailThread, attempt.thread_id)
    if thread is None:
        return None

    message = _match_message(db, attempt=attempt, thread_id=thread.id)
    if message is None:
        db.rollback()
        return None

    outcome = _settle_match(db, attempt=attempt, thread_id=thread.id, message=message)
    if outcome is None:
        # R-1: a genuine settlement anomaly (see _settle_match) -- nothing
        # was fenced/resolved, so there's nothing to classify either.
        return None

    classified = False
    # Phase 2 (Codex review): distinct from `classified` -- only
    # `_OUTCOME_NO_VERDICT` counts here (see `_classify_and_stamp`'s own
    # docstring for why `_OUTCOME_FAILED`/`_OUTCOME_RACE_LOST` deliberately
    # don't). This is the ONE place a BYOK-failure-left-unclassified message
    # from the reconciliation path becomes visible to the ingest stats the
    # frontend actually reads.
    left_unclassified = False
    if (
        attempt.provider == "outlook"
        and classify_messages
        and attempt.verified_at is None
    ):
        classify_outcome = _classify_and_stamp(
            db,
            attempt_id=attempt.id,
            message=message,
            thread_id=thread.id,
            user_id=thread.user_id,
            breaker=breaker,
            left_unclassified_ids=left_unclassified_ids,
        )
        classified = classify_outcome in (_OUTCOME_CLASSIFIED, _OUTCOME_ALREADY_CLASSIFIED)
        left_unclassified = classify_outcome == _OUTCOME_NO_VERDICT
    elif attempt.provider == "gmail" and attempt.verified_at is None:
        # Gmail normally stamps verified_at in the send completion
        # transaction (our assembled MIME IS what sync later normalizes,
        # so it's verified the moment it lands) -- this only closes the gap
        # left by a completion transaction that crashed after the provider
        # call succeeded but before its own commit.
        db.execute(select(MailThread).where(MailThread.id == thread.id).with_for_update())
        stamp_verified(db, attempt_id=attempt.id)
        db.commit()

    outcome["classified"] = classified
    outcome["left_unclassified"] = left_unclassified
    return outcome


def run_reconciliation_pass(
    db: Session,
    *,
    provider_account_id: UUID,
    provider: str,
    classify_messages: bool,
    breaker: ClassificationBreaker | None = None,
    # Run-shared MUTABLE set of provider_message_ids already attempted (and
    # left no-verdict) in this ingest run -- read to skip duplicate wire
    # calls AND fed by this module's own no-verdict outcomes, so a
    # START-pass failure protects the page loop and the END pass alike
    # (final Codex pass). None = no ingest-run context (the standalone
    # reconcile_reply_attempt task).
    left_unclassified_ids: set[str] | None = None,
) -> dict:
    """One level pass for one account: fetch every reconciliation-eligible
    attempt, then reconcile each as its own isolated unit. Safe to call at
    ingest START, ingest END, and from `reconcile_reply_attempt` -- every
    step is idempotent, so re-running against already-settled rows is a
    cheap no-op.

    Timeliness-only (this module's own docstring): a failure OUTSIDE the
    per-attempt loop below -- the eligible-attempts query itself, or
    anything else unexpected -- must never propagate to the four ingest
    call sites. Aborting a sync before it's fetched anything (the START
    pass) or failing an already-committed run (the END pass) over a
    reconciliation-only concern would be exactly the gating this function's
    docstring promises never happens. Rolled back, logged, and reported as
    nothing-done instead of raised.

    `breaker` and `left_unclassified_ids` (plan: phase 3 of
    the LLM-failure work) pass straight through to `_reconcile_one_attempt`
    -> `_classify_and_stamp` -- see that function's docstring. Both default
    to "no context" so `reconcile_reply_attempt` (the standalone Celery task,
    with no ingest run behind it) is unaffected.
    """
    try:
        attempt_ids = _eligible_attempt_ids(
            db, provider_account_id=provider_account_id, provider=provider
        )
        completed = 0
        classified = 0
        # Phase 2 (Codex review, D-C/D-I): forwarded into ingest's own
        # `stats["left_unclassified"]` at both outlook_ingest.py call sites
        # -- previously this pass's own no-verdict outcomes were silently
        # dropped, invisible to the ingest toast/auto-sync warning even
        # though the page-loop's own `_upsert_page_messages` counter already
        # reported the analogous case.
        left_unclassified = 0
        for attempt_id in attempt_ids:
            try:
                outcome = _reconcile_one_attempt(
                    db,
                    attempt_id=attempt_id,
                    classify_messages=classify_messages,
                    breaker=breaker,
                    left_unclassified_ids=left_unclassified_ids,
                )
            except SoftTimeLimitExceeded:
                # Never swallowed as a per-attempt failure (final Codex
                # pass): Celery is telling the whole TASK to stop, and
                # grinding through the remaining attempts -- each a
                # potential paid retry chain -- until the hard kill is the
                # opposite of stopping. _classify_and_stamp already rolled
                # back its own work before re-raising.
                raise
            except Exception:
                db.rollback()
                logger.exception(
                    "reply reconciliation pass failed for attempt %s", attempt_id
                )
                continue
            if outcome is None:
                continue
            completed += 1
            if outcome.get("classified"):
                classified += 1
            if outcome.get("left_unclassified"):
                left_unclassified += 1
        return {
            "attempts_checked": len(attempt_ids),
            "completed": completed,
            "classified": classified,
            "left_unclassified": left_unclassified,
        }
    except SoftTimeLimitExceeded:
        # The never-propagate contract above is about reconciliation-only
        # concerns; the soft time limit is the TASK's own stop signal and
        # must reach it (final Codex pass).
        try:
            db.rollback()
        except Exception:
            logger.exception(
                "rollback failed during soft-limit handling for account %s",
                provider_account_id,
            )
        raise
    except Exception:
        db.rollback()
        logger.exception(
            "reply reconciliation pass failed for account %s", provider_account_id
        )
        return {"attempts_checked": 0, "completed": 0, "classified": 0, "left_unclassified": 0}
