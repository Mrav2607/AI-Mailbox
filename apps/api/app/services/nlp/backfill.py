"""Batch classification ("backfill") shared by the HTTP route and the Celery
worker. Small runs execute inline in the request and big ones get queued, and
both paths funnel through run_backfill so their behavior can't drift.

Also home to the two shared "what is a thread's latest message" queries, since
every caller that answers that question has to answer it the *same* way or the
bucket a thread lands in stops matching the message we show and label for it."""

from uuid import UUID
from typing import Any, Sequence

from sqlalchemy import select, desc, func
from sqlalchemy.orm import Session

from app.db.models import MailThread, MailMessage, Classification
from app.services.nlp.classifier import classify, build_classification_text
from app.services.nlp.persistence import upsert_classification


def latest_message_ordering():
    """The one true "newest message wins" ordering. Coalesce rather than plain
    NULLS LAST: a message with no sent_at falls back to created_at, and every
    consumer has to agree on that or they'll disagree about which message is a
    thread's latest."""
    return (
        func.coalesce(MailMessage.sent_at, MailMessage.created_at).desc().nullslast(),
        MailMessage.created_at.desc(),
    )


def latest_label_subquery():
    """Correlated scalar subquery yielding a thread's current bucket: the label
    of the latest classification on the thread's latest message, or NULL when
    that message is unclassified. Lets the triage/count queries filter by bucket
    *before* applying a row limit, so a bucket view isn't starved by more-recent
    threads that happen to land in other buckets."""
    latest_message = (
        select(MailMessage.id)
        .where(MailMessage.thread_id == MailThread.id)
        .order_by(*latest_message_ordering())
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


def latest_messages_by_thread(
    db: Session, thread_ids: Sequence[UUID], *, columns: Sequence[Any]
) -> dict[UUID, Any]:
    """Map each thread id to its latest message, selecting only ``columns``.

    Postgres does the picking via DISTINCT ON, which matters: we used to pull
    every message of every listed thread -- whole rows, body_text, body_html and
    the headers JSONB -- and then drop all but the newest in Python. That's
    megabytes over the wire to render a page of snippets.

    ``columns`` must include ``MailMessage.thread_id``; it's what we key on.
    """
    if not thread_ids:
        return {}
    rows = db.execute(
        select(*columns)
        .where(MailMessage.thread_id.in_(thread_ids))
        # DISTINCT ON keeps the first row per thread_id, so the ordering below
        # decides which one that is -- and it's the same ordering the bucket
        # filter uses.
        .distinct(MailMessage.thread_id)
        .order_by(MailMessage.thread_id, *latest_message_ordering())
    ).all()
    return {row.thread_id: row for row in rows}


def run_backfill(
    db: Session,
    user_id: UUID,
    *,
    limit: int,
    force: bool = False,
    bucket: str = "unclassified",
    backend: str | None = None,
    include_task_counts: bool = False,
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
    latest_message_by_thread = latest_messages_by_thread(
        db,
        thread_ids,
        columns=(
            MailMessage.id,
            MailMessage.thread_id,
            MailMessage.snippet,
            MailMessage.body_text,
        ),
    )

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
    task_created = sum(
        message_id not in already_classified for message_id, _text in to_classify
    )
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

    result = {
        "status": "ok",
        "created": created,
        "scanned": scanned,
    }
    if include_task_counts:
        result["task_created"] = task_created
        result["task_processed"] = len(to_classify)
    return result
