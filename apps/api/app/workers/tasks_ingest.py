from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, cast
from uuid import UUID

import redis
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.engine import CursorResult

from .celery_app import celery_app
from app.core.config import settings
from app.core.logging import logger
from app.db.base import SessionLocal
from app.db.models import MailSyncRun, MailThread, ProviderAccount
from app.services.ingest.gmail_ingest import ingest_gmail_messages
from app.services.sync_runs import renew_sync, start_sync_run


_HEARTBEAT_INTERVAL_SECONDS = 60
# How long finished runs stick around. Long enough to debug last week's
# incident, short enough that a 5-minute cadence doesn't accumulate forever.
_RUN_RETENTION = timedelta(days=14)


# A 500-message pull with classification can legitimately run for many
# minutes, so the time limit is generous -- it's a backstop against a wedged
# Gmail/DB call pinning the worker forever, not a performance target.
@celery_app.task(
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1740,
)
def ingest_gmail_for_user(
    self,
    run_id: str,
    user_id: str,
    max_results: int = 25,
    skip_existing: bool = True,
    classify_messages: bool = True,
    new_only: bool = False,
) -> dict:
    """Pull the user's Gmail messages in the background.

    This used to run inline in the request handler, where up to 500 serial
    Gmail fetches would pin a threadpool worker and a DB connection for
    minutes. Now the route just enqueues us and returns 202.

    Transient failures (network blips, Gmail rate limits, DB hiccups) retry
    with backoff; since skip_existing is the norm, a retry resumes where the
    failed run left off instead of re-pulling everything. ValueError is the
    one non-retryable case -- we catch it below and report it as a user error.
    """
    run_uuid = UUID(run_id)

    def set_state(
        status: str, *, error: str | None = None, result: dict | None = None
    ) -> bool:
        with SessionLocal() as state_db:
            run = state_db.get(MailSyncRun, run_uuid)
            if not run:
                return False
            if (
                status in ("running", "retrying")
                and run.status in ("succeeded", "failed")
            ):
                return False
            renew_sync(state_db, run, status)
            if run.started_at is None:
                run.started_at = datetime.now(timezone.utc)
            run.error = error
            run.result = result
            if status in ("succeeded", "failed"):
                run.completed_at = datetime.now(timezone.utc)
            state_db.commit()
            return True

    if not set_state("running"):
        return {
            "status": "error",
            "user_id": user_id,
            "detail": "sync run is no longer active",
        }
    last_heartbeat = monotonic()

    def heartbeat() -> None:
        # Renew on a timer, not once per thread: the lease is 40 minutes, so a
        # write per thread was hundreds of pointless transactions per pull. We
        # stamp the clock even when set_state says the run is gone, or a
        # finalized run would go right back to hitting the DB every thread.
        nonlocal last_heartbeat
        now = monotonic()
        if now - last_heartbeat < _HEARTBEAT_INTERVAL_SECONDS:
            return
        last_heartbeat = now
        set_state("running")

    try:
        with SessionLocal() as db:
            result = ingest_gmail_messages(
                db=db,
                user_id=user_id,
                max_results=max_results,
                skip_existing=skip_existing,
                classify_messages=classify_messages,
                new_only=new_only,
                progress=heartbeat,
            )
    except ValueError as exc:
        payload = {"status": "error", "user_id": user_id, "detail": str(exc)}
        set_state("failed", error=str(exc), result=payload)
        return payload
    except Exception as exc:
        if self.request.retries < self.max_retries:
            set_state("retrying", error="transient sync failure")
            raise self.retry(exc=exc, countdown=2 ** self.request.retries) from exc
        set_state("failed", error="sync failed after retries")
        raise

    payload = {"status": "ok", "user_id": user_id, **result}
    set_state("succeeded", result=payload)
    return payload


# Redis key holding the dispatcher's last check-in. Liveness has to be measured
# by "did the scheduler run", not "did a scheduled sync succeed": the dispatcher
# correctly skips users who already have a run in flight, and with the browser
# fallback still syncing, those skips are the common case. Keying liveness on
# scheduled runs would report a perfectly healthy scheduler as dead.
DISPATCHER_HEARTBEAT_KEY = "sync:dispatcher:heartbeat"


def _heartbeat_ttl_seconds() -> int:
    # Outlive a few missed cycles so a slow tick isn't read as death, but still
    # expire on its own -- an absent key is the "scheduler is gone" signal.
    return max(60, settings.scheduled_sync_interval_seconds * 3)


def write_dispatcher_heartbeat() -> None:
    client = redis.from_url(settings.redis_url)
    try:
        client.set(
            DISPATCHER_HEARTBEAT_KEY,
            datetime.now(timezone.utc).isoformat(),
            ex=_heartbeat_ttl_seconds(),
        )
    finally:
        client.close()


def read_dispatcher_heartbeat() -> datetime | None:
    client = redis.from_url(settings.redis_url)
    try:
        raw = client.get(DISPATCHER_HEARTBEAT_KEY)
    finally:
        client.close()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(
            raw.decode() if isinstance(raw, bytes) else str(raw)
        )
    except ValueError:
        return None


def _eligible_provider_rows(db) -> list[tuple[UUID, UUID]]:
    """(user_id, provider_id) for every Gmail account the scheduler should pull.

    Eligible means: connected, has a refresh token, not paused for reauth, and
    past the first-ingest baseline. That baseline is a history cursor OR a known
    Gmail thread -- cursor alone counts, because an account whose first ingest
    found an empty mailbox still has one, and requiring a thread would strand it
    forever (no threads -> no new-only pull -> no first thread).

    Judged against each user's OLDEST Gmail account, because that's the one
    ingest_gmail_messages actually loads. Filtering first and deduping after
    would let a user with a paused oldest account and a healthy newer one look
    eligible via the newer row, while every run ingest actually started picked
    the paused one and died -- a doomed sync every cycle, forever. Until runs
    carry an account identity, dispatcher and ingest have to agree on which
    account they mean.
    """
    oldest_per_user = (
        select(ProviderAccount)
        .where(ProviderAccount.provider == "gmail")
        .distinct(ProviderAccount.user_id)
        .order_by(ProviderAccount.user_id, ProviderAccount.created_at)
        .subquery()
    )
    account = aliased(ProviderAccount, oldest_per_user)
    # Correlated to the alias, not ProviderAccount -- the bare table isn't in
    # this query any more, so correlating to it would compare against the wrong
    # row (or nothing at all).
    has_thread = (
        select(MailThread.id)
        .where(
            MailThread.user_id == account.user_id,
            MailThread.provider == "gmail",
        )
        .exists()
    )
    rows = db.execute(
        select(account.user_id, account.id).where(
            account.refresh_token.is_not(None),
            account.sync_paused_at.is_(None),
            or_(account.gmail_history_id.is_not(None), has_thread),
        )
    ).all()
    return [(user_id, provider_id) for user_id, provider_id in rows]


@celery_app.task(ignore_result=True, time_limit=120)
def dispatch_scheduled_syncs() -> dict:
    """Queue a new-only pull for every connected mailbox. Runs on the beat.

    Makes sync independent of an open browser tab. It only ever
    enqueues while the real work happens in ingest_gmail_for_user.
    """
    # First, before anything that can fail: the heartbeat is how the beat
    # container's healthcheck and the sync-health endpoint know the scheduler is
    # alive. A per-user Gmail problem must not read as a dead scheduler.
    write_dispatcher_heartbeat()

    dispatched = 0
    skipped = 0
    failed = 0
    with SessionLocal() as db:
        candidates = _eligible_provider_rows(db)
        for user_id, _provider_id in candidates:
            try:
                run, deduplicated = start_sync_run(
                    db,
                    user_id,
                    mode="scheduled",
                    options={
                        "max_results": settings.scheduled_sync_max_results,
                        "skip_existing": True,
                        "classify_messages": True,
                        "new_only": True,
                    },
                )
                if deduplicated:
                    # Someone already holds the slot (a manual run, or the
                    # browser fallback). Nothing to do -- next tick is minutes
                    # away.
                    skipped += 1
                else:
                    dispatched += 1
            except Exception:
                # One user's bad state must never stop the others from syncing.
                failed += 1
                logger.exception("scheduled sync dispatch failed for user %s", user_id)

        _log_stale_mailboxes(db, [user_id for user_id, _ in candidates])

    return {"dispatched": dispatched, "skipped": skipped, "failed": failed}


def _log_stale_mailboxes(db, user_ids: list[UUID]) -> None:
    """Shout if a mailbox hasn't had a successful sync in too long.
    """
    if not user_ids:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.sync_stale_after_seconds
    )
    rows = db.execute(
        select(MailSyncRun.user_id, func.max(MailSyncRun.completed_at))
        .where(MailSyncRun.user_id.in_(user_ids), MailSyncRun.status == "succeeded")
        .group_by(MailSyncRun.user_id)
    ).all()
    newest = {user_id: completed for user_id, completed in rows}
    for user_id in user_ids:
        last = newest.get(user_id)
        if last is not None and last < cutoff:
            logger.error(
                "mailbox stale: no successful sync for user %s since %s",
                user_id,
                last.isoformat(),
            )


@celery_app.task(ignore_result=True, time_limit=300)
def prune_sync_runs() -> dict:
    """Drop old finished runs. Hourly, not per dispatch.

    Keeps each user's newest successful run whatever its age: it's what
    last_succeeded_at reads, so pruning it would create a stale mailbox.
    """
    cutoff = datetime.now(timezone.utc) - _RUN_RETENTION
    # Each user's newest successful run, resolved inside the same statement as
    # the delete so there's no window where the anchor is computed but not yet
    # protected.
    anchors = (
        select(MailSyncRun.id)
        .distinct(MailSyncRun.user_id)
        .where(MailSyncRun.status == "succeeded")
        .order_by(MailSyncRun.user_id, MailSyncRun.completed_at.desc())
        .scalar_subquery()
    )
    with SessionLocal() as db:
        result = cast(
            CursorResult[Any],
            db.execute(
                delete(MailSyncRun).where(
                    MailSyncRun.status.in_(("succeeded", "failed")),
                    MailSyncRun.completed_at < cutoff,
                    MailSyncRun.id.notin_(anchors),
                )
            ),
        )
        deleted = result.rowcount
        db.commit()
    return {"deleted": deleted or 0}
