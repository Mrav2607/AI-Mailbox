from __future__ import annotations

from uuid import UUID

from .celery_app import celery_app
from app.db.base import SessionLocal
from app.db.models import MailThread, MailMessage
from app.services.nlp.backfill import run_backfill
from app.services.nlp.classifier import classify, build_classification_text
from app.services.nlp.persistence import upsert_classification


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
