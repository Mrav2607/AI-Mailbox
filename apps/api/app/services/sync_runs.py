from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


def start_sync_run(
    db: Session, user_id: UUID, *, mode: str, options: dict
) -> tuple[MailSyncRun, bool]:
    """Claim the user's single sync slot and enqueue the work.

    Returns (run, deduplicated). A truthy `deduplicated` means someone else
    already owns the slot and `run` is their run, not a new one.

    Both the ingest route and the scheduler come through here so there is one
    implementation of the single-flight dance -- the `uq_mail_sync_run_active_user`
    partial index is the referee, and the IntegrityError branch is what makes a
    lost race return the winner instead of a 500.
    """
    existing = active_sync(db, user_id)
    if existing:
        return existing, True

    now = now_utc()
    run = MailSyncRun(
        id=uuid4(),
        user_id=user_id,
        mode=mode,
        status="queued",
        options=options,
        lease_expires_at=now + QUEUED_SYNC_LEASE,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = active_sync(db, user_id)
        if winner:
            return winner, True
        raise

    # Imported here, not at module scope: app.workers.tasks_ingest imports this
    # module, so a top-level import would close the cycle.
    from app.workers.tasks_ingest import ingest_gmail_for_user

    # Enqueue and recording the task id fail for different reasons and must be
    # handled differently -- catching both together is how a run that's actually
    # executing gets marked failed.
    try:
        task = cast(Any, ingest_gmail_for_user).delay(
            run_id=str(run.id),
            user_id=str(user_id),
            max_results=options["max_results"],
            skip_existing=options["skip_existing"],
            classify_messages=options["classify_messages"],
            new_only=options["new_only"],
        )
    except Exception:
        # Nothing is running, and the committed row holds the user's only sync
        # slot -- release it or the mailbox is wedged until the lease expires.
        db.rollback()
        run.status = "failed"
        run.error = "failed to enqueue sync"
        run.completed_at = now_utc()
        db.commit()
        raise

    try:
        run.task_id = task.id
        db.commit()
    except Exception:
        # The task IS queued; only our note of its id didn't land. Marking the
        # run failed here would be a lie with teeth: 'failed' is terminal, so it
        # both releases the slot while the ingest is still running (letting the
        # scheduler start a second concurrent run for the same user) and makes
        # the worker abort on set_state's terminal guard.
        #
        # Leave it 'queued' and let the worker take it from there -- it moves
        # queued->running normally. The only casualty is task_id staying null,
        # and nothing user-facing reads it (the console polls by run_id).
        db.rollback()
        raise
    return run, False


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
