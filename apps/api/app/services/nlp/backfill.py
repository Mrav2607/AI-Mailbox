"""Batch classification ("backfill") shared by the HTTP route and the Celery
worker. Small runs execute inline in the request and big ones get queued, and
both paths funnel through run_backfill so their behavior can't drift."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, desc, func
from sqlalchemy.orm import Session

from app.db.models import MailThread, MailMessage, Classification
from app.services.nlp.classifier import classify, build_classification_text
from app.services.nlp.persistence import upsert_classification


def latest_label_subquery():
    """Correlated scalar subquery yielding a thread's current bucket: the label
    of the latest classification on the thread's latest message, or NULL when
    that message is unclassified. Lets the triage/count queries filter by bucket
    *before* applying a row limit, so a bucket view isn't starved by more-recent
    threads that happen to land in other buckets."""
    latest_message = (
        select(MailMessage.id)
        .where(MailMessage.thread_id == MailThread.id)
        .order_by(
            # Coalesce rather than NULLS LAST: the Python-side picks (here in
            # run_backfill, and in the triage assembly) treat a missing sent_at
            # as "fall back to created_at", and this SQL pick has to agree or
            # the bucket filter and the displayed label can disagree about
            # which message is a thread's latest.
            func.coalesce(MailMessage.sent_at, MailMessage.created_at)
            .desc()
            .nullslast(),
            MailMessage.created_at.desc(),
        )
        .limit(1)
        # Correlate explicitly: this is nested two levels deep, so without it
        # SQLAlchemy pulls mail_thread into this subquery's own FROM (turning the
        # correlation into a cross join) instead of binding to the outer thread.
        .correlate(MailThread)
        .scalar_subquery()
    )
    return (
        select(Classification.label)
        .where(Classification.message_id == latest_message)
        .order_by(desc(Classification.created_at))
        .limit(1)
        .scalar_subquery()
    )


def run_backfill(
    db: Session,
    user_id: UUID,
    *,
    limit: int,
    force: bool = False,
    bucket: str = "unclassified",
    backend: str | None = None,
) -> dict:
    """Classify (or, with ``force``, re-classify) the latest message of up to
    ``limit`` of the user's threads currently in ``bucket``.

    Assumes bucket/backend were already validated -- the route checks both
    before running inline or enqueuing the worker task.
    """
    query = select(MailThread).where(MailThread.user_id == user_id)
    if bucket == "unclassified":
        query = query.where(latest_label_subquery().is_(None))
    elif bucket != "all":
        query = query.where(latest_label_subquery() == bucket)

    threads = (
        db.execute(
            query.order_by(
                MailThread.last_message_at.desc().nullslast(),
                MailThread.created_at.desc(),
            ).limit(limit)
        )
        .scalars()
        .all()
    )
    thread_ids = [t.id for t in threads]
    messages = (
        db.execute(
            select(MailMessage)
            .where(MailMessage.thread_id.in_(thread_ids))
            # Same coalesced ordering as latest_label_subquery: the loop below
            # picks the max itself, but on exact-timestamp ties it keeps the
            # first row it sees, so the DB order decides -- and it has to
            # decide the same way the bucket filter does.
            .order_by(
                func.coalesce(MailMessage.sent_at, MailMessage.created_at)
                .desc()
                .nullslast(),
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
    # Only a row with an actual label counts as classified. upsert_classification
    # can persist label=None, and the bucket filter treats that as unclassified
    # (the subquery yields NULL) -- so if we skipped on mere row existence, a
    # null-label message would sit in "unclassified" forever, unreachable
    # without force.
    already_classified = (
        set(
            db.execute(
                select(Classification.message_id).where(
                    Classification.message_id.in_(message_ids),
                    Classification.label.is_not(None),
                )
            ).scalars()
        )
        if message_ids
        else set()
    )

    # Skip the (expensive) classify call when a label already exists and we're
    # not forcing a refresh. We snapshot plain values here so the loop below
    # never touches ORM state that gets expired by the batch commits.
    to_classify = [
        (
            message.id,
            build_classification_text(
                subject_by_thread.get(message.thread_id),
                message.snippet,
                message.body_text,
            ),
        )
        for message in latest_message_by_thread.values()
        if force or message.id not in already_classified
    ]
    scanned = len(latest_message_by_thread)
    # Close the read transaction before classifying -- classify() can block on
    # a Gemini call or local inference, and we don't want to sit
    # idle-in-transaction on a pooled connection while that runs.
    db.commit()

    batch_size = 25
    created = 0
    pending: list[tuple[UUID, str | None, float | None, str | None, str | None]] = []

    def flush_pending() -> None:
        nonlocal created
        for message_id, label, confidence, rationale, model_version in pending:
            upsert_classification(
                db,
                message_id=message_id,
                label=label,
                confidence=confidence,
                rationale=rationale,
                model_version=model_version,
            )
        db.commit()
        created += len(pending)
        pending.clear()

    # Classify outside any transaction, then commit results in small batches so
    # a late classifier failure only loses the current batch, not the whole run.
    for message_id, text_for_classification in to_classify:
        label, confidence, rationale, model_version = classify(
            text_for_classification, backend=backend
        )
        pending.append((message_id, label, confidence, rationale, model_version))
        if len(pending) >= batch_size:
            flush_pending()
    if pending:
        flush_pending()

    return {
        "status": "ok",
        "created": created,
        "scanned": scanned,
    }
