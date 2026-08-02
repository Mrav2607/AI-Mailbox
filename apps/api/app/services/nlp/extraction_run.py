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

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.base import SessionLocal
from app.db.models import ActionItem, Classification, MailMessage, MailThread
from app.services.nlp.extractor import ACTION_LABELS, NoAction, extract_action
from app.services.nlp.persistence import (
    MAX_ATTEMPTS,
    PENDING_LEASE,
    claim_action_item,
    claimable_predicate,
    record_extraction,
)
from app.services.nlp.providers import (
    LlmCredential,
    extraction_feature_enabled,
    resolve_extraction_credential,
)

_DEFAULT_SINCE_DAYS = 30
_RESULT_BUCKETS = ("extracted", "no_action", "ineligible", "failed", "skipped")


def _empty_counts() -> dict:
    return {"processed": 0, **{bucket: 0 for bucket in _RESULT_BUCKETS}}


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


def _claim_extract_record(
    db: Session,
    message_id: UUID,
    *,
    force: bool = False,
    credential: LlmCredential | None = None,
) -> tuple[str, UUID | None]:
    """One claim -> extract -> record cycle for ``message_id``.

    Returns ``(bucket, user_id)`` -- ``bucket`` is one of
    extracted/no_action/ineligible/failed/skipped/unavailable, and
    ``user_id`` is the message's owner when known (``None`` if the message
    or its thread has vanished), so callers can attach it to a task result
    without a second query.

    ``credential`` is the already-resolved credential for a sweep's user
    (``run_extraction_sweep`` resolves it once per run, before the loop, and
    passes it to every call). ``None`` means the caller doesn't know the
    user yet -- ``run_extraction_for_message``'s user id only becomes known
    once the thread loads below -- so this function resolves it itself,
    still before the claim; an unresolvable credential returns bucket
    ``"unavailable"`` with zero attempts spent (no claim ever issued).
    """
    message = db.get(MailMessage, message_id)
    if message is None:
        return "skipped", None

    # Frozen lock order: MailThread -> ActionItem. This FOR UPDATE also
    # serializes against a concurrent set_thread_done -- whichever commits
    # second sees the other's row/flag, so no interleaving leaves an open
    # row on a done thread.
    thread = db.execute(
        select(MailThread).where(MailThread.id == message.thread_id).with_for_update()
    ).scalar_one_or_none()
    if thread is None:
        return "skipped", None

    thread_done = thread.done_at is not None
    subject = thread.subject
    sender = message.sender
    snippet = message.snippet
    body_text = message.body_text
    received_at = message.sent_at or message.created_at
    user_id = thread.user_id

    if credential is None:
        credential = resolve_extraction_credential(db, user_id).credential
        if credential is None:
            # Nothing was claimed, so a plain commit (not a rollback) is
            # enough to release the thread lock we just took.
            db.commit()
            return "unavailable", user_id

    claim_token = claim_action_item(
        db,
        message_id=message.id,
        thread_id=thread.id,
        user_id=user_id,
        thread_done=thread_done,
        force=force,
    )
    # Release the thread lock before the (possibly slow) LLM call -- no DB
    # transaction is checked out while extract_action runs.
    db.commit()
    if claim_token is None:
        return "skipped", user_id

    try:
        result = extract_action(
            subject=subject,
            sender=sender,
            snippet=snippet,
            body_text=body_text,
            received_at=received_at,
            credential=credential,
        )

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

        # Re-read done_at -- a concurrent set_thread_done could have
        # committed while the LLM call was in flight.
        current_done_at = db.execute(
            select(MailThread.done_at).where(MailThread.id == thread.id)
        ).scalar_one_or_none()

        recorded = record_extraction(
            db,
            message_id=message.id,
            claim_token=claim_token,
            result=result,
            thread_done=current_done_at is not None,
            label_still_actionable=label_still_actionable,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        try:
            _fence_claim_to_failed(message.id, claim_token)
        except Exception as fencing_exc:
            # The claim is stuck pending with no fence -- a task that
            # swallowed this would look like success to Celery's autoretry
            # and end the retry chain on a row nothing will ever reclaim
            # until the recovery tick's lease expires. Propagate the
            # ORIGINAL failure, not the fencing one.
            raise exc from fencing_exc
        return "failed", user_id

    if not recorded:
        # Our lease expired and someone else reclaimed the row before this
        # write landed -- a stale, harmless no-op, not a bug.
        logger.warning(
            "action extraction record fenced out (stale claim_token) for message %s",
            message.id,
        )
        return "skipped", user_id

    if not label_still_actionable:
        return "ineligible", user_id
    if isinstance(result, NoAction):
        return "no_action", user_id
    if result is None:
        return "failed", user_id
    return "extracted", user_id


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

    bucket, user_id = _claim_extract_record(db, message_id)
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
    db: Session, user_id: UUID, *, since_days: int, limit: int, force: bool
) -> list[UUID]:
    """Never-claimed or currently-claimable messages: the user's messages
    whose classification label is actionable, thread not done, within
    ``since_days``, newest first. An existing row must also be claimable
    (``force``-widened) -- otherwise a settled/live-pending message would
    burn a selection slot for nothing.

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
                or_(ActionItem.id.is_(None), claimable_predicate(force=force)),
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
    the flag being off. The resolved credential then threads through every
    ``_claim_extract_record`` call this sweep makes.
    """
    if not extraction_feature_enabled():
        return _disabled_result()

    credential = resolve_extraction_credential(db, user_id).credential
    if credential is None:
        return _disabled_result()

    terminalize_expired_pending(db, user_id=user_id)

    if recovery:
        message_ids = _recovery_candidates(db, user_id, limit=limit)
    else:
        message_ids = _message_driven_candidates(
            db, user_id, since_days=since_days, limit=limit, force=force
        )

    counts = _empty_counts()
    for message_id in message_ids:
        counts["processed"] += 1
        try:
            bucket, _user_id = _claim_extract_record(
                db, message_id, force=force, credential=credential
            )
        except Exception:
            # _claim_extract_record already fences its OWN pre-claim/
            # containment failures to a `failed` row and re-raises only when
            # even that fencing failed -- this is the outer fan-out
            # isolation (same idiom as dispatch_scheduled_syncs): one bad
            # message must never abort the sweep for the rest. Roll back
            # first, or every later candidate this sweep would fail with
            # PendingRollbackError on the shared session.
            db.rollback()
            logger.exception(
                "action extraction sweep failed for message %s", message_id
            )
            counts["failed"] += 1
            continue
        counts[bucket] += 1
    return {"status": "ok", **counts}


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
