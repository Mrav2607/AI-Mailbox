"""Persistence helpers for classification results and the action-extraction
claim/attempt state machine.

`upsert_classification` is a single upsert keyed on ``message_id`` so every
write path (inline ingest, backfill, and the Celery workers) stays race-safe
and idempotent: if two classifiers reach the same message concurrently, the
second updates the row instead of crashing on the unique constraint.

`claim_action_item` / `record_extraction` are the same idea applied to a
longer-lived attempt: a claim reserves ``message_id``'s row for one worker's
extraction call, and the matching record either settles it or -- fenced on
``claim_token`` -- no-ops if the claim has since expired and been stolen.
Neither commits; the caller (``extraction_run``) owns the transaction
boundary so the claim can release its lock before the (possibly slow) LLM
call runs, mirroring ``backfill.py``'s snapshot-then-release pattern.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, case, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import ActionItem, Classification
from app.services.nlp.extractor import ExtractedAction, NoAction

# A claim's lease: past this age, a fresh worker may steal a still-"pending"
# row (the one that claimed it is presumed dead -- crashed, killed, or lost
# its Celery lease).
PENDING_LEASE = timedelta(minutes=30)
MAX_ATTEMPTS = 3

# Marks a label set by a human in the console rather than a model -- lets
# every write path (this module, the route, backfill) agree on the string
# without importing it from a route. `upsert_classification`'s write-time
# guard checks against this to decide whether a model path is allowed to
# clobber it.
OPERATOR_MODEL_VERSION = "user-override"

# Bounded snapshot cap for classification_feedback.input_text: the first
# FEEDBACK_TEXT_MAX chars of build_classification_text(...)'s output. Matches
# the LLM paths' own 6,000-char cap and sits ~4x the local model's 256-token
# window, so it's generous enough to cover what either classifier actually
# saw without storing unbounded email bodies forever.
FEEDBACK_TEXT_MAX = 6000


def upsert_classification(
    db: Session,
    *,
    message_id: UUID,
    label: str | None,
    confidence: float | None,
    rationale: str | None,
    model_version: str | None,
    overwrite_user_override: bool = False,
) -> str:
    """Insert a classification for ``message_id``, or overwrite the existing one.

    Does not commit -- the caller controls the transaction boundary so it can
    batch writes.

    ``overwrite_user_override`` gates whether this write is allowed to clobber
    an operator's manual label. Every model-driven path (backfill, both
    ingests, the Celery classify task) leaves it ``False``, which adds a
    ``WHERE classification.model_version IS DISTINCT FROM 'user-override'`` to
    the conflict update -- so a user override that lands mid-run survives even
    when the model path finishes after it. Only the reclassify route itself
    passes ``True``.

    Returns ``"written"`` if the row was inserted or updated, or
    ``"protected"`` if the conflict hit the override guard and nothing
    changed (distinguished via the statement's rowcount: 1 vs 0).
    """
    values = {
        "message_id": message_id,
        "label": label,
        "confidence": confidence,
        "rationale": rationale,
        "model_version": model_version,
    }
    stmt = insert(Classification).values(**values).on_conflict_do_update(
        constraint="uq_classification_message",
        set_={
            "label": label,
            "confidence": confidence,
            "rationale": rationale,
            "model_version": model_version,
        },
        where=(
            None
            if overwrite_user_override
            else Classification.model_version.is_distinct_from(OPERATOR_MODEL_VERSION)
        ),
    )
    # preserve_rowcount matters: without it, psycopg3 reports -1 (truthy!) for
    # INSERT..ON CONFLICT statements, and every guarded write would read as
    # "written" even when the override guard blocked it. Set on the statement
    # (not as an execute() kwarg) so callers and test fakes keep the plain
    # single-argument execute signature.
    result = db.execute(stmt.execution_options(preserve_rowcount=True))
    return "written" if result.rowcount else "protected"


def claimable_predicate(*, force: bool = False):
    """WHERE-clause fragment: is an EXISTING ``action_item`` row a valid claim
    target right now?

    Settled rows (``extracted`` / ``no_action``) are terminal; only
    ``ineligible`` (a reclassify-back must always be able to re-extract),
    ``failed`` under the attempt cap, and an expired ``pending`` lease under
    the cap remain retryable. ``force`` widens this to any non-pending row --
    an operator backfill can re-run a settled extraction, but force must
    never steal a claim a live worker currently holds.
    """
    lease_cutoff = datetime.now(timezone.utc) - PENDING_LEASE
    base = or_(
        ActionItem.outcome == "ineligible",
        and_(ActionItem.outcome == "failed", ActionItem.attempts < MAX_ATTEMPTS),
        and_(
            ActionItem.outcome == "pending",
            ActionItem.last_attempted_at < lease_cutoff,
            ActionItem.attempts < MAX_ATTEMPTS,
        ),
    )
    if force:
        return or_(base, ActionItem.outcome != "pending")
    return base


def _done_stamp_columns(thread_done: bool, now: datetime) -> dict:
    """Conditional status stamp shared by claim and record: a done thread
    resolves an ``open`` row to ``done`` at write time, but must never
    rewrite a row an operator already resolved (e.g. ``dismissed``) -- so the
    CASE reads the row's OWN pre-write status, never a constant.
    """
    if not thread_done:
        return {}
    return {
        "status": case((ActionItem.status == "open", "done"), else_=ActionItem.status),
        "status_at": case(
            (ActionItem.status == "open", now), else_=ActionItem.status_at
        ),
    }


def claim_action_item(
    db: Session,
    *,
    message_id: UUID,
    thread_id: UUID,
    user_id: UUID,
    thread_done: bool,
    force: bool = False,
) -> UUID | None:
    """Atomically claim ``message_id``'s ``action_item`` row for one
    extraction attempt.

    Returns a fresh ``claim_token`` on success, ``None`` if nothing is
    claimable right now (a live pending claim someone else owns). Does not
    commit. When ``thread_done`` and the row's existing ``status`` is
    ``"open"``, additionally stamps ``status="done"`` -- a worker crash
    between claim and record must never leave an open row on a done thread,
    where it would surface as an obligation after an auto-reopen. A forced
    reclaim of a dismissed row must preserve ``dismissed`` byte-for-byte.
    """
    now = datetime.now(timezone.utc)
    token = uuid4()

    insert_values = {
        "message_id": message_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "outcome": "pending",
        "claim_token": token,
        "attempts": 1,
        "last_attempted_at": now,
    }
    if thread_done:
        # Fresh insert -- there is no prior status to preserve, so the
        # unconditional stamp (not the CASE used below) is correct here.
        insert_values["status"] = "done"
        insert_values["status_at"] = now

    insert_stmt = (
        insert(ActionItem)
        .values(**insert_values)
        .on_conflict_do_nothing(constraint="uq_action_item_message")
    )
    if db.execute(insert_stmt).rowcount:
        return token

    # Someone already has a row for this message -- try to (re)claim it.
    update_values = {
        "outcome": "pending",
        "claim_token": token,
        "attempts": ActionItem.attempts + 1,
        "last_attempted_at": now,
        **_done_stamp_columns(thread_done, now),
    }
    claim_stmt = (
        update(ActionItem)
        .where(ActionItem.message_id == message_id, claimable_predicate(force=force))
        .values(**update_values)
    )
    if db.execute(claim_stmt).rowcount:
        return token

    # Not claimable -- but if it's a pending claim that expired AT the
    # attempt cap, it's not retryable either, and nothing else ever settles
    # it without this: terminalize it to failed. Disjoint from the claim
    # predicate above (that one requires attempts < cap), so this never
    # steals a claim the update just above could have taken.
    terminalize_stmt = (
        update(ActionItem)
        .where(
            ActionItem.message_id == message_id,
            ActionItem.outcome == "pending",
            ActionItem.last_attempted_at < now - PENDING_LEASE,
            ActionItem.attempts >= MAX_ATTEMPTS,
        )
        .values(outcome="failed")
    )
    db.execute(terminalize_stmt)
    return None


def record_extraction(
    db: Session,
    *,
    message_id: UUID,
    claim_token: UUID,
    result: ExtractedAction | NoAction | None,
    thread_done: bool,
    label_still_actionable: bool,
    already_replied: bool = False,
) -> bool:
    """Fenced write of an extraction attempt's outcome.

    ``UPDATE ... WHERE message_id = :m AND claim_token = :t AND
    outcome = 'pending'`` -- a stale worker's late write (its lease expired
    and someone else already reclaimed the row) affects zero rows and the
    caller treats that as a logged no-op, never overwriting a newer attempt.
    Does not commit.

    Precedence: a label that left ``ACTION_LABELS`` while the call was in
    flight always wins (records ``ineligible``, claimable, so a
    reclassify-back can re-extract) over the call's own result. Otherwise the
    result decides: ``ExtractedAction`` -> ``extracted``, ``NoAction`` ->
    ``no_action`` (extraction fields cleared -- a forced re-extraction that
    finds nothing must not leave stale fields from a prior attempt), ``None``
    -> ``failed`` (fields untouched, transient failure).

    Independent of which branch above fires: this record also resolves the
    row's existing ``"open"`` status to ``"done"`` when either ``thread_done``
    -- an open row created on a done thread must not resurface after an
    auto-reopen -- OR ``already_replied`` AND the result is a
    ``kind="reply"`` extraction (docs/plans/2026-08-13-reply-plan.md §3.5:
    the caller has already checked this message's ``created_at`` against
    ``mail_thread.replied_at``, so an extraction that only now discovers
    "needs a reply" on a message the user already answered from CortexMail
    records itself pre-resolved, never a second obligation for an answered
    message). Status is never reset otherwise; re-extraction must not
    resurrect an item an operator resolved.
    """
    now = datetime.now(timezone.utc)
    values: dict = {"claim_token": None}

    if not label_still_actionable:
        values["outcome"] = "ineligible"
    elif isinstance(result, ExtractedAction):
        values.update(
            outcome="extracted",
            kind=result.kind,
            title=result.title,
            due_at=result.due_at,
            due_precision=result.due_precision,
            due_raw=result.due_raw,
            amount=result.amount,
            currency=result.currency,
            source_confidence=result.confidence,
            model_version=result.model_version,
        )
    elif isinstance(result, NoAction):
        values.update(
            outcome="no_action",
            kind=None,
            title=None,
            due_at=None,
            due_precision=None,
            due_raw=None,
            amount=None,
            currency=None,
            source_confidence=None,
            model_version=None,
        )
    else:
        values["outcome"] = "failed"

    resolves_already_answered = (
        already_replied and isinstance(result, ExtractedAction) and result.kind == "reply"
    )
    values.update(_done_stamp_columns(thread_done or resolves_already_answered, now))

    stmt = (
        update(ActionItem)
        .where(
            ActionItem.message_id == message_id,
            ActionItem.claim_token == claim_token,
            ActionItem.outcome == "pending",
        )
        .values(**values)
    )
    return bool(db.execute(stmt).rowcount)
