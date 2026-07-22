from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, cast
from uuid import UUID

import redis
from sqlalchemy import delete, func, or_, select
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
    provider_account_id: str | None = None,
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
            "provider_account_id": provider_account_id,
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
                provider_account_id=provider_account_id,
                max_results=max_results,
                skip_existing=skip_existing,
                classify_messages=classify_messages,
                new_only=new_only,
                progress=heartbeat,
            )
    except ValueError as exc:
        payload = {
            "status": "error",
            "user_id": user_id,
            "provider_account_id": provider_account_id,
            "detail": str(exc),
        }
        set_state("failed", error=str(exc), result=payload)
        return payload
    except Exception as exc:
        if self.request.retries < self.max_retries:
            set_state("retrying", error="transient sync failure")
            raise self.retry(exc=exc, countdown=2 ** self.request.retries) from exc
        set_state("failed", error="sync failed after retries")
        raise

    payload = {
        "status": "ok",
        "user_id": user_id,
        "provider_account_id": provider_account_id,
        **result,
    }
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


# Built on first use rather than at import: redis-py connection pools don't
# survive a fork, and this module is imported before the prefork worker forks
# its children. Lazily, each process ends up with its own.
_redis_client: redis.Redis | None = None


def _heartbeat_redis() -> redis.Redis:
    """One pooled client per process.

    Every health poll from every open tab reads the heartbeat, and from_url()
    builds a fresh ConnectionPool each call -- that's a TCP connection set up
    and torn down per read.
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url)
    return _redis_client


def write_dispatcher_heartbeat() -> None:
    _heartbeat_redis().set(
        DISPATCHER_HEARTBEAT_KEY,
        datetime.now(timezone.utc).isoformat(),
        ex=_heartbeat_ttl_seconds(),
    )


def read_dispatcher_heartbeat() -> datetime | None:
    try:
        raw = _heartbeat_redis().get(DISPATCHER_HEARTBEAT_KEY)
    except redis.RedisError:
        # Redis being down is exactly when the health endpoint gets hit, so it
        # must degrade, not 500. "Can't tell" reads the same as "no heartbeat":
        # scheduler_alive goes false, which is the honest answer. The cached
        # client may hold a dead socket after a Redis restart, so drop it and
        # let the next call rebuild the pool.
        global _redis_client
        _redis_client = None
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(
            raw.decode() if isinstance(raw, bytes) else str(raw)
        )
    except ValueError:
        return None


def _eligible_provider_rows(db) -> list[tuple[UUID, UUID]]:
    """(user_id, provider_account_id) for every Gmail account the scheduler should pull.

    Eligible means: connected, has a refresh token, not paused for reauth, and
    past the first-ingest baseline. That baseline is a history cursor OR a known
    Gmail thread -- cursor alone counts, because an account whose first ingest
    found an empty mailbox still has one, and requiring a thread would strand it
    forever (no threads -> no new-only pull -> no first thread).

    Every eligible account gets its own row now, not just a user's oldest one.
    Runs carry their provider_account_id straight through to
    ingest_gmail_messages, so a paused account can no longer hide a healthy
    sibling (or vice versa) behind a single ambiguous per-user slot.
    """
    has_thread = (
        select(MailThread.id)
        .where(MailThread.provider_account_id == ProviderAccount.id)
        .exists()
    )
    rows = db.execute(
        select(ProviderAccount.user_id, ProviderAccount.id).where(
            ProviderAccount.provider == "gmail",
            ProviderAccount.refresh_token.is_not(None),
            ProviderAccount.sync_paused_at.is_(None),
            or_(ProviderAccount.gmail_history_id.is_not(None), has_thread),
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
        for user_id, account_id in candidates:
            try:
                run, deduplicated = start_sync_run(
                    db,
                    user_id,
                    account_id,
                    mode="scheduled",
                    options={
                        "max_results": settings.scheduled_sync_max_results,
                        "skip_existing": True,
                        "classify_messages": True,
                        "new_only": True,
                    },
                )
                if deduplicated:
                    # Someone already holds this account's slot (a manual run,
                    # or the browser fallback). Nothing to do -- next tick is
                    # minutes away.
                    skipped += 1
                else:
                    dispatched += 1
            except Exception:
                # One account's bad state must never stop the others from
                # syncing. An unhandled commit error (a connection blip, not
                # the handled IntegrityError) leaves the shared session in an
                # aborted transaction, so every later account this tick would
                # fail with PendingRollbackError -- roll back to clear it.
                db.rollback()
                failed += 1
                logger.exception(
                    "scheduled sync dispatch failed for user %s account %s",
                    user_id,
                    account_id,
                )

        _log_stale_mailboxes(db, candidates)

    return {"dispatched": dispatched, "skipped": skipped, "failed": failed}


def _log_stale_mailboxes(db, candidates: list[tuple[UUID, UUID]]) -> None:
    """Shout if a mailbox hasn't had a successful sync in too long.

    Anchored per provider_account_id, not per user: a healthy account must
    never hide a stale sibling connected to the same user.
    """
    if not candidates:
        return
    account_ids = [account_id for _, account_id in candidates]
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.sync_stale_after_seconds
    )
    rows = db.execute(
        select(MailSyncRun.provider_account_id, func.max(MailSyncRun.completed_at))
        .where(
            MailSyncRun.provider_account_id.in_(account_ids),
            MailSyncRun.status == "succeeded",
        )
        .group_by(MailSyncRun.provider_account_id)
    ).all()
    newest = {account_id: completed for account_id, completed in rows}
    for user_id, account_id in candidates:
        last = newest.get(account_id)
        if last is not None and last < cutoff:
            logger.error(
                "mailbox stale: no successful sync for user %s account %s since %s",
                user_id,
                account_id,
                last.isoformat(),
            )


@celery_app.task(ignore_result=True, time_limit=300)
def prune_sync_runs() -> dict:
    """Drop old finished runs. Hourly, not per dispatch.

    Keeps each account's newest successful run whatever its age: it's what
    last_succeeded_at reads, so pruning it would create a stale mailbox. Runs
    predating the account_id migration have a NULL provider_account_id and
    can't anchor anything -- they're always fair game for the age-based
    delete below.
    """
    cutoff = datetime.now(timezone.utc) - _RUN_RETENTION
    # Each account's newest successful run, resolved inside the same statement
    # as the delete so there's no window where the anchor is computed but not
    # yet protected.
    anchors = (
        select(MailSyncRun.id)
        .distinct(MailSyncRun.provider_account_id)
        .where(
            MailSyncRun.status == "succeeded",
            MailSyncRun.provider_account_id.is_not(None),
        )
        # nullslast: a succeeded row shouldn't have a null completed_at, but if
        # one ever did, NULLS FIRST (Postgres' default for DESC) would make it
        # the "anchor" and expose the real newest success to pruning.
        .order_by(
            MailSyncRun.provider_account_id, MailSyncRun.completed_at.desc().nullslast()
        )
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
