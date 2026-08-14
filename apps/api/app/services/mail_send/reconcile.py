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

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.models import MailMessage, MailThread, ReplyAttempt
from app.services.mail_send.common import advance_fence_and_resolve, mark_sent, stamp_verified
from app.services.nlp.classifier import build_classification_text, classify_with_usage
from app.services.nlp.persistence import upsert_classification
from app.services.nlp.providers import ClassificationRouter
from app.services.nlp.usage import UsageAccumulator

# Every status the partial index (`ix_reply_attempt_reconcile`) covers --
# kept identical to the index predicate on purpose, so "the index matches
# every reconciliation-eligible row and nothing else" is a real invariant,
# not just a comment (plan §3.5, P6-5).
_BLOCKING_STATUSES = ("preparing", "inflight", "unknown", "abandoned")


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
) -> dict:
    """Phase 1, its own transaction: settle `sent` + fence + action
    resolution. Runs first and unconditionally on a match, regardless of
    whether classification later succeeds."""
    db.execute(select(MailThread).where(MailThread.id == thread_id).with_for_update())
    if attempt.status != "sent":
        mark_sent(db, attempt_id=attempt.id, provider_message_id=message.provider_message_id)
    resolved = advance_fence_and_resolve(
        db, thread_id=thread_id, attempt_created_at=attempt.created_at
    )
    db.commit()
    return {"resolved_action_items": resolved}


def _classify_and_stamp(
    db: Session, *, attempt_id: UUID, message: MailMessage, thread_id: UUID, user_id: UUID
) -> bool:
    """Phase 2, ITS OWN transaction, isolated from phase 1: the
    classification claim (P8-2). Takes the shared serialization point
    (`MailThread` -> `ReplyAttempt` lock order, the same one used by every
    reconciliation path), re-reads `verified_at`, and only the null-winner
    classifies and stamps -- guaranteeing exactly one classifier invocation
    even when two workers race this same attempt. A classification failure
    rolls back only this attempt's work and leaves `verified_at` NULL for a
    later pass; it never touches phase 1's already-committed settlement.
    """
    db.execute(select(MailThread).where(MailThread.id == thread_id).with_for_update())
    current_verified_at = db.execute(
        select(ReplyAttempt.verified_at).where(ReplyAttempt.id == attempt_id).with_for_update()
    ).scalar_one_or_none()
    if current_verified_at is not None:
        # Someone else already won this race (or a prior pass already
        # verified it) -- nothing to do.
        db.rollback()
        return False

    try:
        text_for_classification = build_classification_text(
            None, message.snippet, message.body_text
        )
        router = ClassificationRouter(user_id)
        routing = router.routing_for(db)
        attempt_result = classify_with_usage(text_for_classification, routing=routing)
        label, confidence, rationale, model_version = attempt_result.verdict
        if routing.mode == "user" and routing.credential is not None:
            acc = UsageAccumulator(user_id)
            acc.record(
                "classification",
                routing.credential.provider,
                attempt_result.usage,
                provider_call_succeeded=attempt_result.provider_call_succeeded,
            )
            acc.flush(db)
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
        return True
    except Exception:
        db.rollback()
        logger.exception(
            "reply reconciliation: classification failed for attempt %s (sent but unverified)",
            attempt_id,
        )
        return False


def _reconcile_one_attempt(
    db: Session, *, attempt_id: UUID, classify_messages: bool
) -> dict | None:
    """One isolated reconciliation unit. Returns `None` if there's nothing
    to do yet (no persisted message matches), else a summary dict."""
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

    classified = False
    if (
        attempt.provider == "outlook"
        and classify_messages
        and attempt.verified_at is None
    ):
        classified = _classify_and_stamp(
            db,
            attempt_id=attempt.id,
            message=message,
            thread_id=thread.id,
            user_id=thread.user_id,
        )
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
    return outcome


def run_reconciliation_pass(
    db: Session, *, provider_account_id: UUID, provider: str, classify_messages: bool
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
    """
    try:
        attempt_ids = _eligible_attempt_ids(
            db, provider_account_id=provider_account_id, provider=provider
        )
        completed = 0
        classified = 0
        for attempt_id in attempt_ids:
            try:
                outcome = _reconcile_one_attempt(
                    db, attempt_id=attempt_id, classify_messages=classify_messages
                )
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
        return {
            "attempts_checked": len(attempt_ids),
            "completed": completed,
            "classified": classified,
        }
    except Exception:
        db.rollback()
        logger.exception(
            "reply reconciliation pass failed for account %s", provider_account_id
        )
        return {"attempts_checked": 0, "completed": 0, "classified": 0}
