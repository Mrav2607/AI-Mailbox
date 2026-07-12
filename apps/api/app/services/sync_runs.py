from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MailSyncRun


ACTIVE_SYNC_STATUSES = ("queued", "running", "retrying")
SYNC_LEASE = timedelta(minutes=40)
QUEUED_SYNC_LEASE = timedelta(hours=2)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def expire_stale_sync(db: Session, user_id: UUID) -> None:
    now = now_utc()
    stale = db.scalar(
        select(MailSyncRun).where(
            MailSyncRun.user_id == user_id,
            MailSyncRun.status.in_(ACTIVE_SYNC_STATUSES),
            MailSyncRun.lease_expires_at < now,
        )
    )
    if stale:
        stale.status = "failed"
        stale.error = "sync lease expired"
        stale.completed_at = now
        db.commit()


def active_sync(db: Session, user_id: UUID) -> MailSyncRun | None:
    expire_stale_sync(db, user_id)
    return db.scalar(
        select(MailSyncRun)
        .where(
            MailSyncRun.user_id == user_id,
            MailSyncRun.status.in_(ACTIVE_SYNC_STATUSES),
        )
        .order_by(MailSyncRun.requested_at.desc())
    )


def renew_sync(db: Session, run: MailSyncRun, status: str | None = None) -> None:
    now = now_utc()
    if status:
        run.status = status
    run.heartbeat_at = now
    run.lease_expires_at = now + SYNC_LEASE


def sync_payload(run: MailSyncRun, *, deduplicated: bool = False) -> dict:
    return {
        "run_id": str(run.id),
        "task_id": run.task_id,
        "mode": run.mode,
        "status": run.status,
        "ready": run.status in ("succeeded", "failed"),
        "deduplicated": deduplicated,
        "result": run.result,
        "error": run.error,
    }
