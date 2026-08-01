from __future__ import annotations

from uuid import UUID

from .celery_app import celery_app
from app.db.base import SessionLocal
from app.db.models import MailThread, MailMessage
from app.services.nlp.backfill import run_backfill
from app.services.nlp.classifier import classify, build_classification_text
from app.services.nlp.extraction_run import (
    extraction_available,
    run_extraction_for_message,
    run_extraction_sweep,
    terminalize_expired_pending,
    users_with_claimable_action_items,
    users_with_unclaimed_actionable_messages,
)
from app.services.nlp.persistence import upsert_classification

# Sweep cap for each recovery-tick pass -- generous enough to drain a normal
# backlog in one tick, cheap enough that a tick never runs long even for a
# user with a lot of retryable work.
_RECOVERY_SWEEP_LIMIT = 25


@celery_app.task
def classify_message(message_id: str) -> dict:
    with SessionLocal() as db:
        message = db.get(MailMessage, UUID(message_id))
        if not message:
            return {"message_id": message_id, "status": "missing"}
        thread = db.get(MailThread, message.thread_id)
        text_for_classification = build_classification_text(
            thread.subject if thread else None,
            message.snippet,
            message.body_text,
        )
        label, confidence, rationale, model_version = classify(text_for_classification)
        upsert_classification(
            db,
            message_id=message.id,
            label=label,
            confidence=confidence,
            rationale=rationale,
            model_version=model_version,
        )
        db.commit()
        return {"message_id": message_id, "label": label, "confidence": confidence}


# Same shape as the ingest task's safeguards: a 500-thread Gemini backfill can
# run a long while, so the time limit is a hung-call backstop, and retries with
# backoff cover transient classifier/DB failures. Already-labeled messages are
# skipped on re-run (unless force), so a retry resumes rather than redoing the
# whole batch.
@celery_app.task(
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
    time_limit=1800,
    soft_time_limit=1740,
)
def backfill_threads_for_user(
    user_id: str,
    limit: int = 100,
    force: bool = False,
    bucket: str = "unclassified",
    backend: str | None = None,
) -> dict:
    """Run a classification backfill off the request path. The backfill route
    enqueues us for anything over its inline cap and returns 202; bucket and
    backend were already validated there."""
    with SessionLocal() as db:
        result = run_backfill(
            db,
            UUID(user_id),
            limit=limit,
            force=force,
            bucket=bucket,
            backend=backend,
        )
    return {"user_id": user_id, **result}


@celery_app.task
def classify_latest_threads(
    user_id: str, limit: int = 25, force: bool = False
) -> dict:
    """Classify the latest message in the user's most recent threads."""
    with SessionLocal() as db:
        result = run_backfill(
            db,
            UUID(user_id),
            limit=limit,
            force=force,
            bucket="all",
            include_task_counts=True,
        )
    # user_id rides along because non-sync tasks don't have a durable ownership
    # row for the task-status endpoint to consult.
    return {
        "status": result["status"],
        "user_id": user_id,
        "created": result["task_created"],
        "processed": result["task_processed"],
    }


# Same autoretry shape as backfill_threads_for_user. It composes with the
# claim state machine: an exception between claim-commit and record-commit
# fences the in-flight claim to failed (claimable) before propagating, so a
# retry's sweep reclaims that row instead of skipping a live pending it owns.
@celery_app.task(
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
    time_limit=1800,
    soft_time_limit=1740,
)
def extract_action_for_message(message_id: str) -> dict:
    """Extract one message's action item -- the reclassify hook's path."""
    with SessionLocal() as db:
        result = run_extraction_for_message(db, UUID(message_id))
    return {"message_id": message_id, **result}


@celery_app.task(
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
    time_limit=1800,
    soft_time_limit=1740,
)
def extract_actions_for_user(
    user_id: str, limit: int = 100, force: bool = False, since_days: int = 30
) -> dict:
    """Run an action-extraction sweep off the request path -- the backfill
    route enqueues us, and the ingest hook fires us after mail lands."""
    with SessionLocal() as db:
        result = run_extraction_sweep(
            db, UUID(user_id), limit=limit, force=force, since_days=since_days
        )
    return {"user_id": user_id, **result}


@celery_app.task(ignore_result=True, time_limit=300)
def extraction_recovery_tick() -> dict:
    """Beat-scheduled safety net (every 900s -- celery_app.py) so retryable
    rows don't depend on new mail arriving to get swept: a quiet sync, a
    zero-upsert run, or SCHEDULED_SYNC_INTERVAL_SECONDS=0 must not strand
    work. Cheap no-op unless extraction_available(); otherwise
    terminalizes every stuck-at-cap pending row (global, not just the users
    swept below -- a user with no OTHER claimable work would never appear in
    either set otherwise), then sweeps two independently derived user sets:

    - row-driven: users owning claimable action_item rows (failed/expired
      pending/reclassified-back ineligible), swept with recovery=True
      (row-driven, no since_days age filter);
    - message-driven: users with an actionable message that has NO
      action_item row at all -- a broker failure that swallowed the ingest
      hook's enqueue leaves no row behind, so the row-driven set alone can
      never see that user; this pass is what recovers them.

    No autoretry -- the next tick is the retry.
    """
    if not extraction_available():
        return {"status": "disabled"}

    with SessionLocal() as db:
        terminalize_expired_pending(db)

        row_driven_users = users_with_claimable_action_items(db)
        for user_id in row_driven_users:
            run_extraction_sweep(db, user_id, limit=_RECOVERY_SWEEP_LIMIT, recovery=True)

        message_driven_users = users_with_unclaimed_actionable_messages(db)
        for user_id in message_driven_users:
            run_extraction_sweep(db, user_id, limit=_RECOVERY_SWEEP_LIMIT)

    return {
        "status": "ok",
        "row_driven_users": len(row_driven_users),
        "message_driven_users": len(message_driven_users),
    }
