from datetime import datetime, timezone
from uuid import UUID
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select, desc, or_, func
from sqlalchemy.orm import Session

from app.core.ratelimit import user_rate_limit
from app.deps import get_db, get_current_user
from app.db.models import MailThread, MailMessage, Classification, AppUser
from app.workers.tasks_ingest import ingest_gmail_for_user
from app.workers.tasks_nlp import backfill_threads_for_user, classify_latest_threads
from app.services.nlp.backfill import latest_label_subquery, run_backfill
from app.services.nlp.classifier import LABELS
from app.services.nlp.persistence import upsert_classification

router = APIRouter(prefix="/mail")

# Upper bounds on caller-supplied counts so a single request can't ask the DB
# (or a Gmail pull) for an unbounded amount of work.
_MAX_PAGE_LIMIT = 200
_MAX_INGEST_RESULTS = 500
# Backfills up to this many threads run inline and return counts immediately;
# anything bigger goes to the Celery worker (a 500-thread Gemini run can hold
# a request open for minutes).
_MAX_INLINE_BACKFILL = 50
# Valid triage filters: every classifier label plus the two synthetic buckets.
_TRIAGE_BUCKETS = frozenset(LABELS) | {"all", "unclassified"}
# Classifier backends a caller may request per run (see services.nlp.classify).
_CLASSIFIER_BACKENDS = frozenset({"local", "gemini", "heuristic", "auto"})
# Marks a label set by a human in the console rather than a model, so it's
# distinguishable from a real prediction and never overwritten by a backfill
# (which only touches unclassified messages unless forced).
_OPERATOR_MODEL_VERSION = "user-override"


class ReclassifyRequest(BaseModel):
    label: str


def _assemble_triage_items(db: Session, threads: list[MailThread]) -> list[dict]:
    """Build triage item dicts for an ordered list of threads: each thread's
    latest message plus that message's latest classification. Shared by the
    triage and search endpoints so both return the same shape."""
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
    latest_classifications = (
        db.execute(
            select(Classification)
            .where(Classification.message_id.in_([m.id for m in messages]))
            .order_by(desc(Classification.created_at))
        )
        .scalars()
        .all()
        if messages
        else []
    )
    classifications_by_msg = {}
    for cls in latest_classifications:
        if cls.message_id not in classifications_by_msg:
            classifications_by_msg[cls.message_id] = cls

    items = []
    for thread in threads:
        latest_message = latest_message_by_thread.get(thread.id)
        classification = classifications_by_msg.get(latest_message.id) if latest_message else None
        items.append(
            {
                "thread_id": str(thread.id),
                "subject": thread.subject,
                "last_message_at": thread.last_message_at,
                "latest_message_snippet": latest_message.snippet if latest_message else None,
                "classification": {
                    "label": classification.label if classification else None,
                    "confidence": float(classification.confidence) if classification and classification.confidence is not None else None,
                    "model_version": classification.model_version if classification else None,
                },
            }
        )
    return items


@router.get("/triage")
def get_triage(
    bucket: str = "needs_reply",
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE_LIMIT),
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """
    Fetch recent threads for the authenticated user with latest classification label.
    """
    if bucket not in _TRIAGE_BUCKETS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid bucket '{bucket}'. Valid: {sorted(_TRIAGE_BUCKETS)}",
        )
    query = select(MailThread).where(MailThread.user_id == current_user.id)
    # Filter by bucket in SQL, before the limit, so a specific-label view returns
    # up to `limit` matching threads instead of whatever matches happen to fall
    # inside the `limit` most-recent threads overall.
    if bucket == "unclassified":
        query = query.where(latest_label_subquery().is_(None))
    elif bucket != "all":
        query = query.where(latest_label_subquery() == bucket)

    threads = list(
        db.execute(
            query.order_by(
                MailThread.last_message_at.desc().nullslast(),
                MailThread.created_at.desc(),
            )
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return {"bucket": bucket, "items": _assemble_triage_items(db, threads)}


@router.get("/counts")
def get_counts(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Total thread count per bucket for the sidebar.

    Grouped in SQL so the counts reflect the whole mailbox rather than a single
    truncated page. Keys cover every triage bucket, including `all` (the total)
    and `unclassified`.
    """
    bucket_label = latest_label_subquery().label("bucket_label")
    grouped = (
        select(bucket_label)
        .select_from(MailThread)
        .where(MailThread.user_id == current_user.id)
        .subquery()
    )
    rows = db.execute(
        select(grouped.c.bucket_label, func.count())
        .group_by(grouped.c.bucket_label)
    ).all()

    counts = {bucket: 0 for bucket in _TRIAGE_BUCKETS}
    total = 0
    for label, count in rows:
        total += count
        if label is None:
            counts["unclassified"] += count
        elif label in counts:
            counts[label] += count
    counts["all"] = total
    return {"counts": counts}


@router.get("/thread/{thread_id}")
def get_thread(
    thread_id: UUID,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    thread = db.get(MailThread, thread_id)
    # 404 (not 403) for another user's thread so we don't leak that it exists.
    if not thread or thread.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = (
        db.execute(
            select(MailMessage)
            .where(MailMessage.thread_id == thread_id)
            .order_by(
                MailMessage.sent_at.desc().nullslast(),
                MailMessage.created_at.desc(),
            )
        )
        .scalars()
        .all()
    )
    return {
        "thread": {
            "id": str(thread.id),
            "subject": thread.subject,
            "provider": thread.provider,
            "last_message_at": thread.last_message_at,
        },
        "messages": [
            {
                "id": str(m.id),
                "sent_at": m.sent_at,
                "sender": m.sender,
                "snippet": m.snippet,
                "body_text": m.body_text,
            }
            for m in messages
        ],
    }


@router.post("/thread/{thread_id}/classification")
def reclassify_thread(
    thread_id: UUID,
    payload: ReclassifyRequest,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Apply an operator's manual label to a thread.

    The console lets the user override the model when QA-ing predictions. The
    label is stored against the thread's latest message (the same message whose
    classification drives the triage view), with full confidence and an
    ``user-override`` model_version. 
    """
    if payload.label not in LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid label '{payload.label}'. Valid: {sorted(LABELS)}",
        )

    thread = db.get(MailThread, thread_id)
    # 404 (not 403) for another user's thread so we don't leak that it exists.
    if not thread or thread.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")

    latest_message = (
        db.execute(
            select(MailMessage)
            .where(MailMessage.thread_id == thread_id)
            .order_by(
                MailMessage.sent_at.desc().nullslast(),
                MailMessage.created_at.desc(),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    if latest_message is None:
        raise HTTPException(status_code=409, detail="Thread has no messages to label")

    upsert_classification(
        db,
        message_id=latest_message.id,
        label=payload.label,
        confidence=1.0,
        rationale="Operator override from the console.",
        model_version=_OPERATOR_MODEL_VERSION,
    )
    db.commit()

    return {
        "thread_id": str(thread_id),
        "classification": {
            "label": payload.label,
            "confidence": 1.0,
            "model_version": _OPERATOR_MODEL_VERSION,
        },
    }


def _escape_like(q: str) -> str:
    """Escape LIKE metacharacters so user input matches literally -- searching
    for "100%" should find "100%", not "100" followed by anything. Backslash
    goes first so we don't double-escape our own escapes."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Generous per-user limit -- the console fires a search per keystroke, but each
# one is an unanchored ILIKE over body_text, so cap runaway callers.
@router.get("/search", dependencies=[Depends(user_rate_limit("search", 60, 60))])
def search_threads(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE_LIMIT),
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Search the user's threads across every bucket.

    Case-insensitive substring match on the thread subject and on any of its
    messages' sender / snippet / body. Returns the same item shape as triage so
    the console can render results in the same list.
    """
    pattern = f"%{_escape_like(q)}%"
    message_match = (
        select(MailMessage.id)
        .where(
            MailMessage.thread_id == MailThread.id,
            or_(
                MailMessage.sender.ilike(pattern, escape="\\"),
                MailMessage.snippet.ilike(pattern, escape="\\"),
                MailMessage.body_text.ilike(pattern, escape="\\"),
            ),
        )
        .exists()
    )
    threads = list(
        db.execute(
            select(MailThread)
            .where(
                MailThread.user_id == current_user.id,
                or_(MailThread.subject.ilike(pattern, escape="\\"), message_match),
            )
            .order_by(
                MailThread.last_message_at.desc().nullslast(),
                MailThread.created_at.desc(),
            )
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return {"query": q, "items": _assemble_triage_items(db, threads)}


@router.delete("/thread/{thread_id}", status_code=204)
def delete_thread(
    thread_id: UUID,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Permanently delete a thread and everything hanging off it.

    The mail_message -> mail_thread and classification -> mail_message foreign
    keys are ON DELETE CASCADE, so dropping the thread row takes its messages
    and their classifications with it.
    """
    thread = db.get(MailThread, thread_id)
    # 404 (not 403) for another user's thread so we don't leak that it exists.
    if not thread or thread.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Thread not found")
    db.delete(thread)
    db.commit()
    return Response(status_code=204)


@router.post(
    "/ingest/gmail",
    status_code=202,
    # Each call queues up to 500 Gmail fetches of Celery work; keep it rare.
    dependencies=[Depends(user_rate_limit("ingest-gmail", 5, 60))],
)
def ingest_gmail(
    max_results: int = Query(default=25, ge=1, le=_MAX_INGEST_RESULTS),
    skip_existing: bool = True,
    classify: bool = True,
    current_user: AppUser = Depends(get_current_user),
) -> dict:
    """Queue a Gmail pull for the worker instead of running it inline.

    A full ingest is up to 500 serial Gmail fetches plus classification --
    way too slow to hold a request open for, so we hand it to Celery and
    return 202 with the task id.
    """
    task = cast(Any, ingest_gmail_for_user).delay(
        user_id=str(current_user.id),
        max_results=max_results,
        skip_existing=skip_existing,
        classify_messages=classify,
    )
    return {"status": "queued", "task_id": task.id}


@router.post(
    "/classify/backfill",
    # Small backfills run the classifier inline; big ones enqueue worker jobs.
    # Either way each call is expensive, so keep the per-user cadence low.
    dependencies=[Depends(user_rate_limit("classify-backfill", 5, 60))],
)
def backfill_classifications(
    response: Response,
    limit: int = Query(default=100, ge=1, le=_MAX_INGEST_RESULTS),
    force: bool = False,
    bucket: str = "unclassified",
    backend: str | None = None,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Classify (or, with ``force``, re-classify) a batch of threads.

    ``bucket`` scopes which threads are eligible, filtered in SQL *before* the
    limit so the batch targets the intended threads instead of whatever falls in
    the most-recent ``limit`` rows:
      - "unclassified" (default): threads whose latest message has no label.
      - a specific label: threads currently in that bucket (needs ``force`` to
        actually re-run, since they're already classified).
      - "all": every thread; unlabeled ones get classified, and with ``force``
        everything is re-classified.
    ``backend`` overrides the configured classifier for this run.

    Up to ``_MAX_INLINE_BACKFILL`` threads this runs inline and returns counts;
    anything bigger is handed to the worker and answered with 202 + task id,
    since a large Gemini batch can hold the request open for minutes.
    """
    if bucket not in _TRIAGE_BUCKETS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid bucket '{bucket}'. Valid: {sorted(_TRIAGE_BUCKETS)}",
        )
    # classify() lowercases its backend anyway, but normalize here so one
    # canonical value flows into task kwargs and logs.
    normalized_backend = backend.lower() if backend is not None else None
    if normalized_backend is not None and normalized_backend not in _CLASSIFIER_BACKENDS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid backend '{backend}'. Valid: {sorted(_CLASSIFIER_BACKENDS)}",
        )

    if limit > _MAX_INLINE_BACKFILL:
        task = cast(Any, backfill_threads_for_user).delay(
            user_id=str(current_user.id),
            limit=limit,
            force=force,
            bucket=bucket,
            backend=normalized_backend,
        )
        response.status_code = 202
        return {"status": "queued", "task_id": task.id}

    return run_backfill(
        db,
        current_user.id,
        limit=limit,
        force=force,
        bucket=bucket,
        backend=normalized_backend,
    )


@router.post(
    "/classify/queue",
    # Same story as ingest: each call piles classification work on the worker.
    dependencies=[Depends(user_rate_limit("classify-queue", 5, 60))],
)
def queue_classification(
    limit: int = Query(default=25, ge=1, le=_MAX_INGEST_RESULTS),
    force: bool = False,
    current_user: AppUser = Depends(get_current_user),
) -> dict:
    task = getattr(classify_latest_threads, "delay")(
        user_id=str(current_user.id),
        limit=limit,
        force=force,
    )
    return {"status": "queued", "task_id": task.id}
