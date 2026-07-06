from __future__ import annotations

from .celery_app import celery_app
from app.db.base import SessionLocal
from app.services.ingest.gmail_ingest import ingest_gmail_messages


# A 500-message pull with classification can legitimately run for many
# minutes, so the time limit is generous -- it's a backstop against a wedged
# Gmail/DB call pinning the worker forever, not a performance target.
@celery_app.task(
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
    time_limit=1800,
    soft_time_limit=1740,
)
def ingest_gmail_for_user(
    user_id: str,
    max_results: int = 25,
    skip_existing: bool = True,
    classify_messages: bool = True,
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
    with SessionLocal() as db:
        try:
            result = ingest_gmail_messages(
                db=db,
                user_id=user_id,
                max_results=max_results,
                skip_existing=skip_existing,
                classify_messages=classify_messages,
            )
        except ValueError as exc:
            # The service raises ValueError when the user hasn't connected a
            # Gmail account -- that's a user problem, not a worker crash.
            return {"status": "error", "user_id": user_id, "detail": str(exc)}
        return {"status": "ok", "user_id": user_id, **result}
