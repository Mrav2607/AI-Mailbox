from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from .celery_app import celery_app
from app.db.base import SessionLocal
from app.db.models import MailSyncRun
from app.services.ingest.gmail_ingest import ingest_gmail_messages
from app.services.sync_runs import renew_sync


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
    try:
        with SessionLocal() as db:
            result = ingest_gmail_messages(
                db=db,
                user_id=user_id,
                max_results=max_results,
                skip_existing=skip_existing,
                classify_messages=classify_messages,
                new_only=new_only,
                progress=lambda: set_state("running"),
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
