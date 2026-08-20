"""Second-stage action extraction: the claim -> extract -> record state
machine, run one message at a time (the reclassify hook) or swept across a
user's messages/rows (backfill, ingest hook, recovery tick).

Lock order, frozen repo-wide for these two tables: ``MailThread`` ->
``ActionItem``. Every transaction touching both locks the thread first with
an explicit ``SELECT ... FOR UPDATE`` -- sessions are ``autoflush=False``, so
an ORM attribute mutation only flushes at COMMIT, after any bulk UPDATE
issued in between it and the commit. Inferring lock order from mutation code
order is therefore not safe; only an explicit ``FOR UPDATE`` establishes it.

Per-message flow mirrors ``backfill.py``'s snapshot-then-release pattern: a
short claim transaction (thread locked, row claimed) commits before the LLM
call runs with no DB transaction checked out, then a short record
transaction (classification row locked, done_at re-read) commits the result.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.base import SessionLocal
from app.db.models import ActionItem, Classification, MailMessage, MailThread
from app.services.nlp.extractor import ACTION_LABELS, NoAction, extract_action_with_usage
from app.services.nlp.llm_client import WORKER_RETRIES
from app.services.nlp.persistence import (
    CREDENTIAL_CLASS_FAILURE_CATEGORIES,
    MAX_ATTEMPTS,
    PENDING_LEASE,
    claim_action_item,
    claimable_predicate,
    record_extraction,
)
from app.services.nlp.providers import (
    CallContext,
    extraction_feature_enabled,
    resolve_extraction_credential,
)
from app.services.nlp.usage import UsageAccumulator

_DEFAULT_SINCE_DAYS = 30
_RESULT_BUCKETS = ("extracted", "no_action", "ineligible", "failed", "skipped")

# The extraction-sweep twin of backfill.py's _CONSECUTIVE_NO_VERDICT_LIMIT
# (plan: phase 3 of the LLM-failure work -- Phase 2 added the classification
# side of this, this closes the same gap for run_extraction_sweep). After
# this many CONSECUTIVE credential-class failures (plan:
# 2026-08-19-extraction-cost-hardening D3 --
# CREDENTIAL_CLASS_FAILURE_CATEGORIES: the SAME credential would fail every
# retry identically), stop issuing more extraction calls for the rest of
# this sweep. Not 1 -- a single odd message shouldn't abort an otherwise-
# healthy sweep. Each attempt this stop prevents can be a real BYOK-billed
# call very likely to fail the same way as the last few.
#
# D3 refines this from the ORIGINAL "three consecutive failed BUCKETS"
# breaker: that version missed the prod retry storm entirely, because the
# storm's calls surfaced under the "skipped" bucket (a genuine attempt whose
# record got fenced out by a competing reclaim), which sailed straight past
# a bucket == "failed" check. The streak now reads the per-attempt failure
# CATEGORY instead, threaded out of `_claim_extract_record` as this
# function's third return value -- a credential-class category feeds the
# streak regardless of which bucket it surfaced under, so a run of
# skipped-but-credential-dead attempts trips it exactly like a run of
# failed-bucket ones would. Resets to zero on anything else: a success, a
# non-credential-class category (rate limits recover on their own), or a
# clean skip with no category at all. It is deliberately NOT fed (and is
# explicitly reset) by a containment/DB failure (run_extraction_sweep's own
# fan-out `except Exception` below) -- those carry no failure_category and
# say nothing about the LLM provider's health, so counting them here would
# misdiagnose a Postgres hiccup as "the user's provider is dead" (Codex
# review, phase 3 should-fix; D3 carries the same reasoning forward).
_CONSECUTIVE_FAILURE_LIMIT = 3


class _DeadlineExhausted(Exception):
    """Raised inside `_claim_extract_record` when the caller's deadline
    expired between the sweep's pre-claim check and the wire call
    (claim-lock waits are invisible to that outer check). Never escapes
    this module -- `_claim_extract_record` catches it, fences the claim
    (nothing was sent, so the row is cleanly retryable), and returns the
    distinct "deadline" bucket so the sweep stops immediately with status
    "timed_out" instead of reporting an "ok" run that silently quit
    (verify pass 3: with the stop disguised as "failed", a sweep stopping
    on its LAST candidate never hit the loop's next-iteration deadline
    check and reported "ok")."""


def _empty_counts() -> dict:
    # failure_categories rides alongside the buckets rather than replacing
    # `failed` -- it's a breakdown (plan: 2026-08-14-llm-failure-visibility),
    # e.g. {"http_429": 8}, populated by run_extraction_sweep only; other
    # callers of _empty_counts() (run_extraction_for_message, the disabled
    # shape) legitimately leave it empty, same as an old result predating
    # this field. `aborted` (plan: 2026-08-19-extraction-cost-hardening D3)
    # is the same kind of additive field: how many candidates the breaker
    # skipped by aborting the rest of a sweep -- always 0 outside
    # run_extraction_sweep's own credential-class trip.
    return {
        "processed": 0,
        **{bucket: 0 for bucket in _RESULT_BUCKETS},
        "failure_categories": {},
        "aborted": 0,
    }


def _disabled_result() -> dict:
    return {"status": "disabled", **_empty_counts()}


def terminalize_expired_pending(db: Session, *, user_id: UUID | None = None) -> None:
    """Flip ``pending`` rows stuck past the claim lease AT the attempt cap to
    terminal ``failed``.

    These rows are deliberately excluded from every claimable predicate (a
    lease past the cap isn't retried, it's terminal) -- without this step
    nothing else ever settles them. Scoped to one user for a per-user sweep,
    or global (``user_id=None``) for the recovery tick, which must settle a
    user's stuck rows even if that user owns no other claimable work and
    would otherwise never show up in either swept user set. Commits (it's
    always the first thing a caller does with a fresh transaction).
    """
    now = datetime.now(timezone.utc)
    conditions = [
        ActionItem.outcome == "pending",
        ActionItem.last_attempted_at < now - PENDING_LEASE,
        ActionItem.attempts >= MAX_ATTEMPTS,
    ]
    if user_id is not None:
        conditions.append(ActionItem.user_id == user_id)
    db.execute(update(ActionItem).where(*conditions).values(outcome="failed"))
    db.commit()


def _call_context_from_resolved(resolved) -> CallContext:
    """Build a `CallContext` from a `ResolvedExtraction`: maps `.source` onto
    `CallContext.payer` -- 'fallback' is the operator's own key, and only a
    'user' source should ever get billed onto the account whose usage we're
    recording (plan §1, §3) -- and carries the resolved row's `credential_id`/
    `revision` straight through (plan: 2026-08-19-extraction-cost-hardening
    D2), so `record_extraction`'s cap-jump can tell whether a rotation raced
    the attempt that used this context. `getattr` with a `None` default,
    not a direct attribute read -- the real `ResolvedExtraction` always
    carries both, but plenty of tests stand in a bare
    ``SimpleNamespace(credential=..., source=...)`` for it, and `None` is
    the exact fallback-credential value anyway (no per-user row to rotate).
    """
    return CallContext(
        credential=resolved.credential,
        payer="user" if resolved.source == "user" else "operator",
        credential_id=getattr(resolved, "credential_id", None),
        revision=getattr(resolved, "revision", None),
    )


def _fence_claim_to_failed(message_id: UUID, claim_token: UUID) -> None:
    """Force an owned claim to terminal ``failed`` on a FRESH session after
    an exception between claim-commit and record-commit.

    A fresh session, not the caller's: the exception may have left the
    caller's session mid-transaction/aborted, and this fencing write must
    succeed independently of whatever broke it. Raises if the fencing commit
    itself fails -- the caller re-raises the ORIGINAL exception in that case,
    since a live pending row must never look like a task that succeeded.
    """
    with SessionLocal() as fresh:
        fresh.execute(
            update(ActionItem)
            .where(
                ActionItem.message_id == message_id,
                ActionItem.claim_token == claim_token,
                ActionItem.outcome == "pending",
            )
            .values(outcome="failed", claim_token=None)
        )
        fresh.commit()


def _rollback_discard_and_fence(
    db: Session, acc: UsageAccumulator, message_id: UUID, claim_token: UUID, exc: BaseException
) -> None:
    """Shared cleanup for every failure branch of `_claim_extract_record`'s
    claim -> extract -> record try block, including `SoftTimeLimitExceeded`
    -- roll back, discard the batched usage (a rolled-back batch must never
    survive into the next flush, or it gets double-counted alongside
    whatever this run records next), and fence the live claim to `failed`
    so a retry's sweep can reclaim it rather than leaving it stuck pending
    for the full `PENDING_LEASE`.

    Raises (chained from the fencing failure) if the fence itself couldn't
    commit -- `exc`, not the fencing exception, is what a caller must
    propagate: the claim is stuck pending with no fence, and swallowing
    THAT would look like success to Celery's autoretry and end the retry
    chain on a row nothing will ever reclaim until the recovery tick's
    lease expires.

    The rollback itself is guarded (final Codex pass): a dead connection
    makes `db.rollback()` raise too, and an unguarded one skipped both the
    discard and the fence -- exactly the stuck-pending-for-a-full-lease
    outcome this helper's docstring promises can't happen. The fence uses
    its own fresh session (`_fence_claim_to_failed`), so it doesn't need
    this session's rollback to have succeeded.
    """
    try:
        db.rollback()
    except Exception:
        logger.exception(
            "rollback failed while fencing extraction claim for message %s", message_id
        )
    acc.discard()
    try:
        _fence_claim_to_failed(message_id, claim_token)
    except Exception as fencing_exc:
        raise exc from fencing_exc


def _claim_extract_record(
    db: Session,
    message_id: UUID,
    *,
    force: bool = False,
    call_context: CallContext | None = None,
    acc: UsageAccumulator | None = None,
    failure_categories: dict[str, int] | None = None,
    deadline: float | None = None,
) -> tuple[str, UUID | None, str | None]:
    """One claim -> extract -> record cycle for ``message_id``.

    Returns ``(bucket, user_id, failure_category)`` -- ``bucket`` is one of
    extracted/no_action/ineligible/failed/skipped/unavailable/deadline;
    ``user_id`` is the message's owner when known (``None`` if the message
    or its thread has vanished), so callers can attach it to a task result
    without a second query; ``failure_category`` (plan:
    2026-08-19-extraction-cost-hardening D3) is the attempt's own
    ``ExtractionAttempt.failure_category`` when a real wire call was made and
    failed, ``None`` otherwise -- including on every SUCCESS bucket, and on
    ``"skipped"`` when no attempt was even made (a lost claim race). This is
    what lets ``run_extraction_sweep``'s breaker read the credential-class
    signal regardless of which bucket a genuine attempt's failure surfaced
    under (a fenced-out record from a competing reclaim still reports its
    real category here, even though its bucket is ``"skipped"``, not
    ``"failed"``).

    ``call_context`` is the already-resolved credential AND who pays for it,
    for a sweep's user (``run_extraction_sweep`` resolves it once per run,
    before the loop, and passes it to every call). ``None`` means the caller
    doesn't know the user yet -- ``run_extraction_for_message``'s user id
    only becomes known once the thread loads below -- so this function
    resolves it itself, still before the claim; an unresolvable credential
    returns bucket ``"unavailable"`` with zero attempts spent (no claim ever
    issued). ``acc`` mirrors this: the sweep's one shared accumulator when
    given, otherwise a fresh one built here once the user id is known --
    that single call is a run of one.

    ``failure_categories`` is an out-param, not (only) the return value: when
    given, a call whose attempt actually failed (``attempt.failure_category``
    set) increments its bucket in place -- this is the sweep's own aggregate
    breakdown across the whole run, distinct from the third return value's
    per-call signal that feeds the streak.
    """
    message = db.get(MailMessage, message_id)
    if message is None:
        return "skipped", None, None

    # Frozen lock order: MailThread -> ActionItem. This FOR UPDATE also
    # serializes against a concurrent set_thread_done -- whichever commits
    # second sees the other's row/flag, so no interleaving leaves an open
    # row on a done thread.
    thread = db.execute(
        select(MailThread).where(MailThread.id == message.thread_id).with_for_update()
    ).scalar_one_or_none()
    if thread is None:
        return "skipped", None, None

    thread_done = thread.done_at is not None
    subject = thread.subject
    sender = message.sender
    snippet = message.snippet
    body_text = message.body_text
    received_at = message.sent_at or message.created_at
    user_id = thread.user_id

    if call_context is None:
        resolved = resolve_extraction_credential(db, user_id)
        if resolved.credential is None:
            # Nothing was claimed, so a plain commit (not a rollback) is
            # enough to release the thread lock we just took.
            db.commit()
            return "unavailable", user_id, None
        call_context = _call_context_from_resolved(resolved)

    if acc is None:
        acc = UsageAccumulator(user_id)

    claim_token = claim_action_item(
        db,
        message_id=message.id,
        thread_id=thread.id,
        user_id=user_id,
        thread_done=thread_done,
        force=force,
        model_version=f"{call_context.credential.provider}:{call_context.credential.model}",
    )
    # Release the thread lock before the (possibly slow) LLM call -- no DB
    # transaction is checked out while extract_action runs.
    db.commit()
    if claim_token is None:
        return "skipped", user_id, None

    # Set once the wire call actually returns, below -- stays None for the
    # deadline-exhausted path (nothing was sent) so every return before that
    # point reports "no attempt, no category" honestly.
    category: str | None = None
    try:
        # Final verify pass: the sweep's pre-claim deadline check can't see
        # time spent WAITING on the claim's own locks -- re-check here,
        # after the claim committed but before anything touches the wire.
        # Nothing ambiguous has been sent yet; the dedicated handler below
        # fences the claim to a cleanly retryable `failed` row and reports
        # the distinct "deadline" bucket.
        if deadline is not None and time.monotonic() >= deadline:
            raise _DeadlineExhausted()
        # WORKER_RETRIES unconditionally, not threaded from a caller: both
        # entry points that ever reach this (run_extraction_for_message,
        # run_extraction_sweep) are Celery-task-only -- extraction has no
        # inline HTTP path (plan: phase 3 of the LLM-failure work) -- so
        # there's no ambiguous context to thread a policy through from the
        # way run_backfill's inline-vs-worker split needs.
        attempt = extract_action_with_usage(
            subject=subject,
            sender=sender,
            snippet=snippet,
            body_text=body_text,
            received_at=received_at,
            credential=call_context.credential,
            policy=WORKER_RETRIES,
        )
        result = attempt.result
        category = attempt.failure_category
        if failure_categories is not None and category is not None:
            failure_categories[category] = failure_categories.get(category, 0) + 1

        # Lock the classification row BEFORE reading its label and hold it
        # through the record commit: a concurrent reclassify-back upserts
        # this same row, so it serializes after our record instead of
        # landing between the read and the write and having nothing left to
        # re-drive extraction.
        classification = db.execute(
            select(Classification)
            .where(Classification.message_id == message.id)
            .with_for_update()
        ).scalar_one_or_none()
        label_still_actionable = (
            classification is not None and classification.label in ACTION_LABELS
        )

        # Re-read done_at/replied_at -- a concurrent set_thread_done or reply
        # send could have committed while the LLM call was in flight.
        current_done_at = db.execute(
            select(MailThread.done_at).where(MailThread.id == thread.id)
        ).scalar_one_or_none()
        current_replied_at = db.execute(
            select(MailThread.replied_at).where(MailThread.id == thread.id)
        ).scalar_one_or_none()
        # The fence (plan §3.5): this message's own DB created_at, never
        # sent_at/received_at -- Postgres clock facts mean created_at is
        # ingest time, not mail chronology, but only CORRELATED console
        # attempts ever advance replied_at, so an inbound message that
        # merely arrived before a batch-ingest replied_at can't mis-resolve
        # here (P3-1's inbound-C case keeps created_at > replied_at).
        already_replied = (
            current_replied_at is not None and message.created_at <= current_replied_at
        )

        recorded = record_extraction(
            db,
            message_id=message.id,
            claim_token=claim_token,
            result=result,
            thread_done=current_done_at is not None,
            label_still_actionable=label_still_actionable,
            already_replied=already_replied,
            model_version=f"{call_context.credential.provider}:{call_context.credential.model}",
            user_id=user_id,
            failure_category=category,
            credential_id=call_context.credential_id,
            credential_revision=call_context.revision,
        )

        # Only a "user" payer gets recorded -- the operator's fallback key
        # is an explicit v1 non-goal (plan §1), and billing it onto the
        # user's readout would be exactly the wrong-payer bug this contract
        # exists to prevent.
        usage_recorded = call_context.payer == "user"
        if usage_recorded:
            acc.record(
                "extraction",
                call_context.credential.provider,
                attempt.usage,
                provider_call_succeeded=attempt.provider_call_succeeded,
            )

        # db.flush() sits OUTSIDE the try: begin_nested() flushes pending ORM
        # state before opening its SAVEPOINT, so a genuine business-write
        # failure would otherwise surface inside the usage handler with the
        # outer transaction already invalid (plan §5). Skip the block
        # entirely when nothing was recorded -- no point opening a SAVEPOINT
        # for an empty batch.
        db.flush()
        if usage_recorded:
            try:
                with db.begin_nested():
                    acc.flush(db)
            except SQLAlchemyError:
                logger.warning(
                    "usage flush failed for message %s; discarding batch",
                    message.id,
                )
                acc.discard()
        db.commit()
        acc.committed()
    except _DeadlineExhausted as exc:
        # Fence exactly like any other mid-claim failure -- the row stays
        # reclaimable -- but report the stop distinctly so the sweep can
        # end with status "timed_out" instead of "ok" (verify pass 3).
        _rollback_discard_and_fence(db, acc, message.id, claim_token, exc)
        return "deadline", user_id, None
    except SoftTimeLimitExceeded as exc:
        # Celery's soft time limit is a plain Exception, so the generic
        # `except Exception` below WOULD otherwise catch it -- and
        # run_extraction_sweep's fan-out loop would then count it as an
        # ordinary per-message failure and keep grinding toward the HARD
        # kill instead of stopping cleanly (Codex review, phase 3 blocker).
        # This must be caught and re-raised BEFORE the generic handler, so
        # the claim still gets fenced exactly like any other mid-call
        # failure (a live pending row must still become claimable-failed),
        # but the signal itself propagates instead of being swallowed.
        _rollback_discard_and_fence(db, acc, message.id, claim_token, exc)
        raise
    except Exception as exc:
        # `category` may already be set here -- the wire call can succeed
        # and THEN something else in this try block (the classification
        # lock, the usage flush) throws. Reporting it is correct: a real
        # credential-class signal from the provider happened, even though
        # this particular row settles via the containment fence rather than
        # a normal record.
        _rollback_discard_and_fence(db, acc, message.id, claim_token, exc)
        return "failed", user_id, category

    if not recorded:
        # Our lease expired and someone else reclaimed the row before this
        # write landed -- a stale, harmless no-op, not a bug. `category`
        # still reports honestly here: the wire call genuinely happened and
        # (if it failed) genuinely failed -- this is the D1 stale-lease/
        # competing-reclaim combination the plan's own repro test drives:
        # bucket "skipped" with a real failure_category attached.
        logger.warning(
            "action extraction record fenced out (stale claim_token) for message %s",
            message.id,
        )
        return "skipped", user_id, category

    if not label_still_actionable:
        return "ineligible", user_id, category
    if isinstance(result, NoAction):
        return "no_action", user_id, category
    if result is None:
        return "failed", user_id, category
    return "extracted", user_id, category


def run_extraction_for_message(db: Session, message_id: UUID) -> dict:
    """Claim -> extract -> record for a single message (the reclassify
    hook's path). Ignores ``since_days`` and the sweep's done-thread cost
    filter -- the hook may target any thread; ``thread_done`` at record time
    handles a done thread correctly by creating the item already resolved.

    The credential can't be resolved here up front -- this message's owner
    is only known once ``_claim_extract_record`` loads its thread -- so it
    resolves internally, still before any claim; an unresolvable credential
    comes back as the ``"unavailable"`` bucket, which this function reports
    the same way as the flag being off: zero attempts, ``disabled`` status.
    """
    if not extraction_feature_enabled():
        return _disabled_result()

    bucket, user_id, _category = _claim_extract_record(db, message_id)
    if bucket == "unavailable":
        return _disabled_result()

    counts = _empty_counts()
    counts["processed"] = 1
    counts[bucket] += 1
    result = {"status": "ok", **counts}
    if user_id is not None:
        result["user_id"] = str(user_id)
    return result


def _message_driven_candidates(
    db: Session,
    user_id: UUID,
    *,
    since_days: int,
    limit: int,
    force: bool,
    model_version: str | None = None,
) -> list[UUID]:
    """Never-claimed or currently-claimable messages: the user's messages
    whose classification label is actionable, thread not done, within
    ``since_days``, newest first. An existing row must also be claimable
    (``force``-widened) -- otherwise a settled/live-pending message would
    burn a selection slot for nothing.

    ``model_version`` (plan: 2026-08-19-extraction-cost-hardening D5) is the
    CURRENT ``f"{provider}:{model}"``, threaded straight to
    ``claimable_predicate`` -- only meaningful when ``force`` is set, since
    that's the only case a settled ``extracted``/``no_action`` row can ever
    become claimable again (comparing against a stale/None model_version
    when ``force`` is off is harmless: that branch of the predicate never
    fires without ``force`` regardless).

    Messages with a NULL ``sent_at`` are possible by design (see
    ``backfill.py``'s ``latest_message_ordering``) -- coalescing to
    ``created_at`` for both the cutoff filter and the ordering keeps them
    from being permanently invisible to this sweep.
    """
    sent_at = func.coalesce(MailMessage.sent_at, MailMessage.created_at)
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    rows = (
        db.execute(
            select(MailMessage.id)
            .join(MailThread, MailThread.id == MailMessage.thread_id)
            .join(Classification, Classification.message_id == MailMessage.id)
            .outerjoin(ActionItem, ActionItem.message_id == MailMessage.id)
            .where(
                MailThread.user_id == user_id,
                MailThread.done_at.is_(None),
                Classification.label.in_(ACTION_LABELS),
                sent_at >= cutoff,
                or_(
                    ActionItem.id.is_(None),
                    claimable_predicate(force=force, model_version=model_version),
                ),
            )
            .order_by(sent_at.desc().nullslast())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows)


def _row_driven_claimable():
    """Row-driven claimable predicate, shared by ``_recovery_candidates`` and
    ``users_with_claimable_action_items``: ``failed`` under the attempt cap,
    an expired ``pending`` lease under the cap, and ``ineligible`` rows whose
    message's CURRENT label is actionable again. Requires a ``Classification``
    outer join for the ineligible branch's label check -- callers must join
    it (on ``ActionItem.message_id``) even though the other two branches
    don't need it.
    """
    lease_cutoff = datetime.now(timezone.utc) - PENDING_LEASE
    return or_(
        and_(ActionItem.outcome == "failed", ActionItem.attempts < MAX_ATTEMPTS),
        and_(
            ActionItem.outcome == "pending",
            ActionItem.last_attempted_at < lease_cutoff,
            ActionItem.attempts < MAX_ATTEMPTS,
        ),
        and_(
            ActionItem.outcome == "ineligible",
            Classification.label.in_(ACTION_LABELS),
        ),
    )


def _recovery_candidates(db: Session, user_id: UUID, *, limit: int) -> list[UUID]:
    """Row-driven, ``since_days``-agnostic: the user's EXISTING claimable
    rows, oldest attempt first, threads not done. Bounded by rows that
    exist, so a lost enqueue on a row-less message is out of scope here (an
    operator backfill covers it); done-thread rows are skipped on purpose --
    they'd record as resolved and invisible, and auto-reopen restores their
    eligibility for a later sweep.
    """
    rows = (
        db.execute(
            select(ActionItem.message_id)
            .join(MailThread, MailThread.id == ActionItem.thread_id)
            .outerjoin(Classification, Classification.message_id == ActionItem.message_id)
            .where(
                ActionItem.user_id == user_id,
                MailThread.done_at.is_(None),
                _row_driven_claimable(),
            )
            .order_by(ActionItem.last_attempted_at.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows)


def run_extraction_sweep(
    db: Session,
    user_id: UUID,
    *,
    limit: int = 100,
    force: bool = False,
    since_days: int = _DEFAULT_SINCE_DAYS,
    recovery: bool = False,
    deadline: float | None = None,
) -> dict:
    """Claim -> extract -> record for up to ``limit`` of a user's candidate
    messages.

    ``recovery=False`` (default) is message-driven: never-claimed or
    currently-claimable messages within ``since_days``. ``recovery=True`` is
    row-driven and ignores ``since_days``: it sweeps existing claimable rows
    regardless of message age, for the beat-scheduled recovery tick.
    Terminalizes this user's expired-at-cap pending rows before selecting
    candidates either way -- otherwise they'd never settle.

    Resolves this user's credential ONCE, after the flag check and before
    any of the above (terminalization included) -- a credential-less user
    (BYOK-only mode with no stored row, or a stored-but-policy-blocked
    custom row) burns zero attempts and gets the same ``disabled`` shape as
    the flag being off. The resolved credential (wrapped in a ``CallContext``
    alongside who pays for it) and one shared ``UsageAccumulator`` for the
    whole run then thread through every ``_claim_extract_record`` call this
    sweep makes.

    ``deadline`` (plan: phase 3 of the LLM-failure work, Codex review
    blocker) is an absolute ``time.monotonic()`` value, threaded from the
    CALLER -- this function never guesses one, same "thread it, don't guess"
    rule as ``RetryPolicy``. It exists because per-message retry budgets
    alone don't bound a sweep where every call keeps SUCCEEDING: with
    ``WORKER_RETRIES`` a single message can legitimately burn up to ~60s of
    waits plus its call time, so a long sweep of all-successful candidates
    can still run for a very long time even though nothing ever trips
    ``_CONSECUTIVE_FAILURE_LIMIT``. Checked once per candidate, BEFORE
    claiming it, so a candidate this stops never gets touched at all --
    this is a proactive backstop, not a replacement for Celery's own soft
    time limit (``SoftTimeLimitExceeded`` is still handled below, and fires
    independently of this check). ``None`` (the default) means unbounded,
    for callers that don't have (or don't need) a caller-side deadline,
    e.g. the reclassify hook's single-message ``run_extraction_for_message``.
    """
    if not extraction_feature_enabled():
        return _disabled_result()

    resolved = resolve_extraction_credential(db, user_id)
    if resolved.credential is None:
        return _disabled_result()

    call_context = _call_context_from_resolved(resolved)
    acc = UsageAccumulator(user_id)
    # D5: what "the current model" means for this run, for
    # claimable_predicate(force=True)'s IS DISTINCT FROM comparison -- the
    # sweep's OWN candidate query is the only claimable_predicate call site
    # that ever runs under force, so this is threaded exactly once, here.
    current_model_version = f"{call_context.credential.provider}:{call_context.credential.model}"

    terminalize_expired_pending(db, user_id=user_id)

    if recovery:
        message_ids = _recovery_candidates(db, user_id, limit=limit)
    else:
        message_ids = _message_driven_candidates(
            db,
            user_id,
            since_days=since_days,
            limit=limit,
            force=force,
            model_version=current_model_version,
        )

    counts = _empty_counts()
    # _CONSECUTIVE_FAILURE_LIMIT's early stop (plan: phase 3 of the
    # LLM-failure work, mirroring Phase 2's run_backfill treatment; refined
    # by D3 -- see this module's _CONSECUTIVE_FAILURE_LIMIT comment for the
    # full "why category, not bucket" reasoning). Reset on anything that
    # isn't a credential-class category, tripped after 3 in a row.
    consecutive_credential_class_failures = 0
    # Distinct from the streak counter: which early-stop tripped, if any,
    # decides the reported status. "llm_unavailable" is a diagnosis about
    # the provider and must only ever come from a genuine credential-class
    # streak; "timed_out" is a neutral, no-diagnosis stop for the deadline
    # backstop and for Celery's own soft time limit (mirrors
    # extraction_recovery_tick's existing status name for the same signal).
    stop_reason: str | None = None
    for index, message_id in enumerate(message_ids):
        if deadline is not None and time.monotonic() >= deadline:
            stop_reason = "timed_out"
            break
        counts["processed"] += 1
        try:
            bucket, _user_id, category = _claim_extract_record(
                db, message_id, force=force, call_context=call_context, acc=acc,
                failure_categories=counts["failure_categories"], deadline=deadline,
            )
        except SoftTimeLimitExceeded:
            # Already fenced by _claim_extract_record -- propagate
            # immediately (before the generic handler below, which would
            # otherwise catch this plain-Exception subclass and count it as
            # an ordinary per-message failure) so Celery's soft-limit
            # handling gets a chance to run instead of grinding on toward
            # the hard kill (Codex review, phase 3 blocker).
            db.rollback()
            raise
        except Exception:
            # _claim_extract_record already fences its OWN pre-claim/
            # containment failures to a `failed` row and re-raises only when
            # even that fencing failed -- this is the outer fan-out
            # isolation (same idiom as dispatch_scheduled_syncs): one bad
            # message must never abort the sweep for the rest. Roll back
            # first, or every later candidate this sweep would fail with
            # PendingRollbackError on the shared session.
            #
            # This is a CONTAINMENT failure -- a DB error, or anything
            # _claim_extract_record itself couldn't recover from -- not an
            # LLM result: it carries no failure_category and must NOT feed
            # the credential-class streak below, or a single Postgres
            # hiccup would misreport as "the user's LLM provider is dead"
            # (Codex review, phase 3 should-fix). Explicitly reset rather
            # than left alone: an unrelated DB blip between two genuine
            # provider failures must not silently bridge them into one
            # continuous trip either.
            db.rollback()
            logger.exception(
                "action extraction sweep failed for message %s", message_id
            )
            counts["failed"] += 1
            consecutive_credential_class_failures = 0
            continue

        if bucket == "deadline":
            # The inner pre-wire check fenced the claim without calling the
            # provider (verify pass 3) -- stop NOW with the truthful status,
            # not "ok": on the sweep's last candidate there is no next
            # iteration for the loop's own deadline check to catch. The
            # candidate wasn't processed (claimed and fenced only), so the
            # optimistic increment above is undone.
            counts["processed"] -= 1
            stop_reason = "timed_out"
            break

        counts[bucket] += 1
        if category in CREDENTIAL_CLASS_FAILURE_CATEGORIES:
            consecutive_credential_class_failures += 1
            if consecutive_credential_class_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                # Every remaining candidate is very likely to fail the same
                # way -- stop spending BYOK-billed attempts on it. The
                # counts collected so far are still exactly right; the loop
                # just never reaches the rest of message_ids -- report how
                # many of them this abort skipped.
                logger.warning(
                    "action extraction sweep for user %s aborting after %d "
                    "consecutive %r failures -- credential looks dead for "
                    "this run",
                    user_id, _CONSECUTIVE_FAILURE_LIMIT, category,
                )
                stop_reason = "llm_unavailable"
                counts["aborted"] = len(message_ids) - (index + 1)
                break
        else:
            consecutive_credential_class_failures = 0
    # Reporting "ok" for a run that quit early would be the same lie this
    # whole plan exists to remove: the counts would be truthful about what was
    # attempted and silent about the candidates never reached. Mirrors
    # run_backfill's status so both jobs say "I stopped" the same way --
    # except run_backfill only ever has one reason to stop early; this has
    # two, and they are NOT interchangeable (see stop_reason's own comment).
    return {"status": stop_reason or "ok", **counts}


def users_with_claimable_action_items(db: Session) -> list[UUID]:
    """Row-driven recovery user set: every user owning at least one claimable
    ``action_item`` row on a not-done thread (``_row_driven_claimable``, the
    same predicate ``_recovery_candidates`` applies per-user)."""
    rows = (
        db.execute(
            select(ActionItem.user_id)
            .join(MailThread, MailThread.id == ActionItem.thread_id)
            .outerjoin(Classification, Classification.message_id == ActionItem.message_id)
            .where(MailThread.done_at.is_(None), _row_driven_claimable())
            .distinct()
        )
        .scalars()
        .all()
    )
    return list(rows)


def users_with_unclaimed_actionable_messages(db: Session) -> list[UUID]:
    """Message-driven recovery user set: every user with at least one
    message whose current classification label is actionable, thread not
    done, within 30 days, and NO ``action_item`` row at all.

    Derived independently of row ownership on purpose -- a user whose only
    actionable message lost its enqueue owns zero ``action_item`` rows and
    is invisible to the row-driven set above, which is exactly the
    broker-failure-before-first-claim case this set exists to recover.
    ``sent_at`` coalesces to ``created_at`` (a NULL ``sent_at`` is possible
    by design) so a message like that isn't invisible here too.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_DEFAULT_SINCE_DAYS)
    rows = (
        db.execute(
            select(MailThread.user_id)
            .join(MailMessage, MailMessage.thread_id == MailThread.id)
            .join(Classification, Classification.message_id == MailMessage.id)
            .outerjoin(ActionItem, ActionItem.message_id == MailMessage.id)
            .where(
                MailThread.done_at.is_(None),
                Classification.label.in_(ACTION_LABELS),
                func.coalesce(MailMessage.sent_at, MailMessage.created_at) >= cutoff,
                ActionItem.id.is_(None),
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    return list(rows)
