from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, desc

from .celery_app import celery_app
from app.db.base import SessionLocal
from app.db.models import MailThread, MailMessage, Classification
from app.services.nlp.classifier import classify


@celery_app.task
def classify_message(message_id: str) -> dict:
    with SessionLocal() as db:
        message = db.get(MailMessage, UUID(message_id))
        if not message:
            return {"message_id": message_id, "status": "missing"}
        text_for_classification = " ".join(
            [
                message.snippet or "",
                message.body_text or "",
            ]
        ).strip()
        label, confidence, rationale, model_version = classify(text_for_classification)
        db.add(
            Classification(
                message_id=message.id,
                label=label,
                confidence=confidence,
                rationale=rationale,
                model_version=model_version,
            )
        )
        db.commit()
        return {"message_id": message_id, "label": label, "confidence": confidence}


@celery_app.task
def classify_latest_threads(
    user_id: str, limit: int = 25, delay_seconds: int = 12, force: bool = False
) -> dict:
    """
    Rate-limited classification: 5 RPM default (12s delay).
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

        message_ids = [m.id for m in latest_message_by_thread.values()]
        existing = (
            db.execute(select(Classification).where(Classification.message_id.in_(message_ids)))
            .scalars()
            .all()
            if message_ids
            else []
        )
        classified_by_id = {c.message_id: c for c in existing}

        created = 0
        processed = 0
        for message in latest_message_by_thread.values():
            existing_cls = classified_by_id.get(message.id)
            if existing_cls and not force:
                continue
            text_for_classification = " ".join(
                [
                    message.snippet or "",
                    message.body_text or "",
                ]
            ).strip()
            label, confidence, rationale, model_version = classify(text_for_classification)
            if existing_cls:
                existing_cls.label = label
                existing_cls.confidence = confidence
                existing_cls.rationale = rationale
                existing_cls.model_version = model_version
            else:
                db.add(
                    Classification(
                        message_id=message.id,
                        label=label,
                        confidence=confidence,
                        rationale=rationale,
                        model_version=model_version,
                    )
                )
                created += 1
            db.commit()
            processed += 1
            if delay_seconds > 0 and processed < len(latest_message_by_thread):
                time.sleep(delay_seconds)

        return {"status": "ok", "created": created, "processed": processed}
