from __future__ import annotations

import importlib
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

# Provider -> the Celery task in app.workers.tasks_ingest that pulls it.
INGEST_TASKS: dict[str, str] = {
    "gmail": "ingest_gmail_for_user",
    "outlook": "ingest_outlook_for_user",
}


def _task_kwargs(provider: str, options: dict) -> dict:
    """Task kwargs for `provider`, dropping options its task doesn't accept.

    Both the ingest route and the scheduler build one generic `options` dict
    shaped like Gmail's (max_results/skip_existing/classify_messages/
    new_only). Outlook's ingest deliberately has no skip_existing/new_only
    knobs -- its delta cursor already makes every bounded run a resumable
    slice of the same walk, so a scheduled "new_only" run for an Outlook
    account is just its normal bounded delta run, not a distinct mode.
    """
    if provider == "outlook":
        return {
            "max_results": options["max_results"],
            "classify_messages": options["classify_messages"],
        }
    return {
        "max_results": options["max_results"],
        "skip_existing": options["skip_existing"],
        "classify_messages": options["classify_messages"],
        "new_only": options["new_only"],
    }


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def expire_stale_sync(db: Session, user_id: UUID) -> None:
    """Fail every one of the user's active runs whose lease has expired.

    A user with multiple Gmail accounts can have several active runs at once,
    so this has to sweep all of them -- stopping at the first would leave the
    rest wedged (holding their account's slot) until their own leases expire.
    """
    now = now_utc()
    stale_runs = db.scalars(
        select(MailSyncRun).where(
            MailSyncRun.user_id == user_id,
            MailSyncRun.status.in_(ACTIVE_SYNC_STATUSES),
            MailSyncRun.lease_expires_at < now,
        )
    ).all()
    if not stale_runs:
        return
    for stale in stale_runs:
        stale.status = "failed"
        stale.error = "sync lease expired"
        stale.completed_at = now
    db.commit()


def active_sync(
    db: Session, user_id: UUID, provider_account_id: UUID | None = None
) -> MailSyncRun | None:
    """The user's most recent active run, optionally scoped to one account.

    `provider_account_id=None` keeps the old cross-account behavior (most
    recent active run for the user, whichever account it belongs to) --
    callers that only care whether *something* is syncing still get that.
    """
    expire_stale_sync(db, user_id)
    stmt = select(MailSyncRun).where(
        MailSyncRun.user_id == user_id,
        MailSyncRun.status.in_(ACTIVE_SYNC_STATUSES),
    )
    if provider_account_id is not None:
        stmt = stmt.where(MailSyncRun.provider_account_id == provider_account_id)
    return db.scalar(stmt.order_by(MailSyncRun.requested_at.desc()))


def active_syncs(db: Session, user_id: UUID) -> list[MailSyncRun]:
    """Every active run across all of the user's accounts, newest first."""
    expire_stale_sync(db, user_id)
    return list(
        db.scalars(
            select(MailSyncRun)
            .where(
                MailSyncRun.user_id == user_id,
                MailSyncRun.status.in_(ACTIVE_SYNC_STATUSES),
            )
            .order_by(MailSyncRun.requested_at.desc())
        )
    )


def start_sync_run(
    db: Session,
    user_id: UUID,
    provider_account_id: UUID,
    *,
    mode: str,
    options: dict,
    provider: str = "gmail",
) -> tuple[MailSyncRun, bool]:
    """Claim the account's single sync slot and enqueue the work.

    Returns (run, deduplicated). A truthy `deduplicated` means someone else
    already owns the slot and `run` is their run, not a new one.

    Both the ingest route and the scheduler come through here so there is one
    implementation of the single-flight dance -- the `uq_mail_sync_run_active_account`
    partial index is the referee, and the IntegrityError branch is what makes a
    lost race return the winner instead of a 500. The slot is per account now,
    not per user: one Gmail account mid-sync must never block a sibling
    account's own run.

    `provider` selects which Celery task actually does the pull (see
    `INGEST_TASKS`) and which of that task's kwargs `options` gets narrowed to
    (see `_task_kwargs`); it defaults to "gmail" for callers that predate
    multi-provider dispatch.
    """
    task_name = INGEST_TASKS.get(provider)
    if task_name is None:
        # Fail before claiming the account's slot -- an unknown provider must
        # never leave a committed 'queued' row with nothing enqueued to
        # release it.
        raise ValueError(f"no ingest task registered for provider {provider!r}")

    existing = active_sync(db, user_id, provider_account_id)
    if existing:
        return existing, True

    now = now_utc()
    run = MailSyncRun(
        id=uuid4(),
        user_id=user_id,
        provider_account_id=provider_account_id,
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
        winner = active_sync(db, user_id, provider_account_id)
        if winner:
            return winner, True
        raise

    # Imported here, not at module scope: app.workers.tasks_ingest imports this
    # module, so a top-level import would close the cycle. importlib (rather
    # than a plain `from app.workers.tasks_ingest import ...`) is what lets
    # the task be picked dynamically by name.
    tasks_ingest = importlib.import_module("app.workers.tasks_ingest")
    task_fn = getattr(tasks_ingest, task_name)

    # Enqueue and recording the task id fail for different reasons and must be
    # handled differently -- catching both together is how a run that's actually
    # executing gets marked failed.
    try:
        task = cast(Any, task_fn).delay(
            run_id=str(run.id),
            user_id=str(user_id),
            provider_account_id=str(provider_account_id),
            **_task_kwargs(provider, options),
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
        "provider_account_id": (
            str(run.provider_account_id) if run.provider_account_id else None
        ),
    }
