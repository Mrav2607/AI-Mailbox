"""Persistence helpers for classification results.

A single upsert keyed on ``message_id`` so every write path (inline ingest,
backfill, and the Celery workers) stays race-safe and idempotent: if two
classifiers reach the same message concurrently, the second updates the row
instead of crashing on the unique constraint.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Classification


def upsert_classification(
    db: Session,
    *,
    message_id: UUID,
    label: str | None,
    confidence: float | None,
    rationale: str | None,
    model_version: str | None,
) -> None:
    """Insert a classification for ``message_id``, or overwrite the existing one.

    Does not commit -- the caller controls the transaction boundary so it can
    batch writes.
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
    )
    db.execute(stmt)
