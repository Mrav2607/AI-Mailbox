from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, desc

from .celery_app import celery_app
from app.db.base import SessionLocal
from app.db.models import MailThread, MailMessage, Classification
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
    """
    Classify the latest message in each of the user's most recent threads.
    """
    with SessionLocal() as db:
        threads = (
            db.execute(
                select(MailThread)
                .where(MailThread.user_id == UUID(user_id))
                .order_by(
                    MailThread.last_message_at.desc().nullslast(),
                    MailThread.created_at.desc(),
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )
        thread_ids = [t.id for t in threads]
        messages = (
            db.execute(
                select(MailMessage)
                .where(MailMessage.thread_id.in_(thread_ids))
                .order_by(
                    MailMessage.sent_at.desc().nullslast(),
                    MailMessage.created_at.desc(),
                )
            )
            .scalars()
            .all()
            if thread_ids
            else []
        )
        latest_message_by_thread: dict[UUID, MailMessage] = {}
        for message in messages:
            message_time = message.sent_at or message.created_at
            if not message_time:
                message_time = datetime.min.replace(tzinfo=timezone.utc)
            current = latest_message_by_thread.get(message.thread_id)
            if not current:
                latest_message_by_thread[message.thread_id] = message
                continue
            current_time = current.sent_at or current.created_at
            if not current_time:
                current_time = datetime.min.replace(tzinfo=timezone.utc)
            if message_time > current_time:
                latest_message_by_thread[message.thread_id] = message

        subject_by_thread = {t.id: t.subject for t in threads}
        message_ids = [m.id for m in latest_message_by_thread.values()]
        already_classified = (
            set(
                db.execute(
                    select(Classification.message_id).where(
                        Classification.message_id.in_(message_ids)
                    )
                ).scalars()
            )
            if message_ids
            else set()
        )

        created = 0
        processed = 0
        for message in latest_message_by_thread.values():
            is_new = message.id not in already_classified
            # Skip the (expensive) classify call when a label already exists and
            # we're not forcing a refresh.
            if not is_new and not force:
                continue
            text_for_classification = build_classification_text(
                subject_by_thread.get(message.thread_id),
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
            if is_new:
                created += 1
            db.commit()
            processed += 1

        # user_id rides along so the task-status endpoint can refuse to hand
        # this result to a different user, same as the other worker tasks.
        return {"status": "ok", "user_id": user_id, "created": created, "processed": processed}
