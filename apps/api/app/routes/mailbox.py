from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.db.models import MailThread, MailMessage, Classification, AppUser
from app.services.ingest.gmail_ingest import ingest_gmail_messages
from app.workers.tasks_nlp import classify_latest_threads
from app.services.nlp.classifier import classify, build_classification_text, LABELS
from app.services.nlp.persistence import upsert_classification

router = APIRouter(prefix="/mail")

# Upper bounds on caller-supplied counts so a single request can't ask the DB
# (or a Gmail pull) for an unbounded amount of work.
_MAX_PAGE_LIMIT = 200
_MAX_INGEST_RESULTS = 500
# Valid triage filters: every classifier label plus the two synthetic buckets.
_TRIAGE_BUCKETS = frozenset(LABELS) | {"all", "unclassified"}
# Marks a label set by a human in the console rather than a model, so it's
# distinguishable from a real prediction and never overwritten by a backfill
# (which only touches unclassified messages unless forced).
_OPERATOR_MODEL_VERSION = "operator-override"


class ReclassifyRequest(BaseModel):
    label: str


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
    threads = (
        db.execute(
            select(MailThread)
            .where(MailThread.user_id == current_user.id)
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
        if bucket != "all":
            if bucket == "unclassified" and classification is not None:
                continue
            if bucket not in ("all", "unclassified"):
                if not classification or classification.label != bucket:
                    continue
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
    return {"bucket": bucket, "items": items}


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


@router.post("/ingest/gmail")
def ingest_gmail(
    max_results: int = Query(default=25, ge=1, le=_MAX_INGEST_RESULTS),
    skip_existing: bool = True,
    classify: bool = True,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = ingest_gmail_messages(
            db=db,
            user_id=str(current_user.id),
            max_results=max_results,
            skip_existing=skip_existing,
            classify_messages=classify,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", **result}


@router.post("/classify/backfill")
def backfill_classifications(
    limit: int = Query(default=100, ge=1, le=_MAX_INGEST_RESULTS),
    force: bool = False,
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    threads = (
        db.execute(
            select(MailThread)
            .where(MailThread.user_id == current_user.id)
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

    subject_by_thread = {t.id: t.subject for t in threads}
    message_ids = [m.id for m in latest_message_by_thread.values()]
    already_classified = (
        set(
            db.execute(
                select(Classification.message_id).where(
                    Classification.message_id.in_(message_ids)
                )
            ).scalars()
        )
        if message_ids
        else set()
    )

    created = 0
    for message in latest_message_by_thread.values():
        is_new = message.id not in already_classified
        # Skip the (expensive) classify call when a label already exists and
        # we're not forcing a refresh.
        if not is_new and not force:
            continue
        text_for_classification = build_classification_text(
            subject_by_thread.get(message.thread_id),
            message.snippet,
            message.body_text,
        )
        label, confidence, rationale, model_version = classify(text_for_classification)
        upsert_classification(
            db,
            message_id=message.id,
            label=label,
            confidence=confidence,
            rationale=rationale,
            model_version=model_version,
        )
        created += 1

    db.commit()
    return {
        "status": "ok",
        "created": created,
        "scanned": len(latest_message_by_thread),
    }


@router.post("/classify/queue")
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
