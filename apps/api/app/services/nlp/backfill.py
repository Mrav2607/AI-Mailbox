"""Batch classification ("backfill") shared by the HTTP route and the Celery
worker. Small runs execute inline in the request and big ones get queued, and
both paths funnel through run_backfill so their behavior can't drift.

Also home to the two shared "what is a thread's latest message" queries, since
every caller that answers that question has to answer it the *same* way or the
bucket a thread lands in stops matching the message we show and label for it."""

from uuid import UUID
from typing import Any, Sequence

from sqlalchemy import select, desc, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.models import MailThread, MailMessage, Classification
from app.services.nlp.classifier import classify_with_usage, build_classification_text
from app.services.nlp.persistence import OPERATOR_MODEL_VERSION, upsert_classification
from app.services.nlp.providers import ClassificationRouter
from app.services.nlp.usage import UsageAccumulator


def latest_message_ordering():
    """The one true "newest message wins" ordering. Coalesce rather than plain
    NULLS LAST: a message with no sent_at falls back to created_at, and every
    consumer has to agree on that or they'll disagree about which message is a
    thread's latest. The trailing id tie-breaker matters too: two messages
    with an identical coalesced timestamp AND created_at (bulk ingest,
    clock resolution) would otherwise resolve to different rows on different
    calls, and the classification-feedback lock (§3.2) needs every caller to
    agree on the SAME latest message or two concurrent corrections can lock
    different rows."""
    return (
        func.coalesce(MailMessage.sent_at, MailMessage.created_at).desc().nullslast(),
        MailMessage.created_at.desc(),
        MailMessage.id.desc(),
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

    ``force`` means "re-classifies model-labeled rows; operator overrides are
    never overwritten": a ``user-override`` row is skipped at candidate
    selection (counted into the returned ``skipped_user_overrides``), and the
    write itself stays guarded by ``upsert_classification``'s conditional
    ``ON CONFLICT`` in case a fresh override lands mid-run.

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
    # One read for both label and provenance -- already_classified decides the
    # non-force skip, user_override_message_ids decides the force-time skip,
    # and neither needs a second round trip to the same rows.
    classification_rows = (
        db.execute(
            select(
                Classification.message_id,
                Classification.label,
                Classification.model_version,
            ).where(Classification.message_id.in_(message_ids))
        ).all()
        if message_ids
        else []
    )
    # Only a row with an actual label counts as classified. upsert_classification
    # can persist label=None, and the bucket filter treats that as unclassified
    # (the subquery yields NULL) -- so if we skipped on mere row existence, a
    # null-label message would sit in "unclassified" forever, unreachable
    # without force.
    already_classified = {
        row.message_id for row in classification_rows if row.label is not None
    }
    user_override_message_ids = {
        row.message_id
        for row in classification_rows
        if row.model_version == OPERATOR_MODEL_VERSION
    }

    # Skip the (expensive) classify call when a label already exists and we're
    # not forcing a refresh; with force, skip user-override rows instead (the
    # write-time guard in upsert_classification is the real protection -- this
    # is the read-time optimization plus honest accounting). We snapshot plain
    # values here so the loop below never touches ORM state that gets expired
    # by the batch commits.
    skipped_user_overrides = 0
    to_classify: list[tuple[UUID, str]] = []
    for message in latest_message_by_thread.values():
        if force:
            if message.id in user_override_message_ids:
                skipped_user_overrides += 1
                continue
        elif message.id in already_classified:
            continue
        to_classify.append(
            (
                message.id,
                build_classification_text(
                    subject_by_thread.get(message.thread_id),
                    message.snippet,
                    message.body_text,
                ),
            )
        )
    scanned = len(latest_message_by_thread)
    # Close the read transaction before classifying -- classify() can block on
    # a Gemini call or local inference, and we don't want to sit
    # idle-in-transaction on a pooled connection while that runs.
    db.commit()

    # One router for the whole run -- its 60s memo keeps a mid-run opt-out
    # effective within a minute without re-resolving routing per message (see
    # ClassificationRouter). One usage accumulator for the whole run too --
    # it gets flushed alongside every batch commit below, never on its own.
    classification_router = ClassificationRouter(user_id)
    acc = UsageAccumulator(user_id)

    batch_size = 25
    created = 0
    # Counted at flush time from actual upsert outcomes, not precomputed from
    # the candidate list -- a mid-run override produces a "protected" outcome,
    # and a precomputed count would report that message as classified anyway.
    task_created = 0
    pending: list[tuple[UUID, str | None, float | None, str | None, str | None]] = []
    usage_pending = False

    # Failure-visibility counters (plan: 2026-08-14-llm-failure-visibility) --
    # read straight off ClassificationAttempt's explicit facts, never derived
    # from routing.mode or provider_call_succeeded (see that dataclass's
    # docstring for why deriving is wrong: a CLASSIFIER_BACKEND=local run
    # never touches an LLM at all, so it must report fell_back=0).
    llm_attempted = 0
    llm_failed = 0
    fell_back = 0
    failure_categories: dict[str, int] = {}

    def flush_pending() -> None:
        nonlocal created, task_created, skipped_user_overrides, usage_pending
        for message_id, label, confidence, rationale, model_version in pending:
            outcome = upsert_classification(
                db,
                message_id=message_id,
                label=label,
                confidence=confidence,
                rationale=rationale,
                model_version=model_version,
            )
            # A "protected" outcome means a user override landed on this
            # message mid-run and the write-time guard held -- that's a skip,
            # not a classification, even though this message cleared the
            # read-time candidate check above.
            if outcome == "written":
                created += 1
                if message_id not in already_classified:
                    task_created += 1
            else:
                skipped_user_overrides += 1

        # db.flush() sits OUTSIDE the try: begin_nested() flushes pending ORM
        # state before opening its SAVEPOINT, so a genuine business-write
        # failure would otherwise surface inside the usage handler with the
        # outer transaction already invalid (plan §5). Skip the block
        # entirely when nothing was recorded this batch -- no point opening
        # a SAVEPOINT for an empty one.
        db.flush()
        if usage_pending:
            try:
                with db.begin_nested():
                    acc.flush(db)
            except SQLAlchemyError:
                logger.warning(
                    "usage flush failed mid-backfill for user %s; discarding batch",
                    user_id,
                )
                acc.discard()
            usage_pending = False
        db.commit()
        acc.committed()
        pending.clear()

    # Classify outside any transaction, then commit results in small batches so
    # a late classifier failure only loses the current batch, not the whole run.
    for message_id, text_for_classification in to_classify:
        routing = classification_router.routing_for(db)
        attempt = classify_with_usage(
            text_for_classification, backend=backend, routing=routing
        )
        label, confidence, rationale, model_version = attempt.verdict
        if attempt.llm_attempted:
            llm_attempted += 1
        if attempt.fallback_used:
            llm_failed += 1
            fell_back += 1
            if attempt.failure_category:
                failure_categories[attempt.failure_category] = (
                    failure_categories.get(attempt.failure_category, 0) + 1
                )
        # `routing.credential` should always be set when mode is "user"
        # (classifier.py's `_classify_llm` degrades to the heuristic if that
        # invariant ever breaks) -- but guard it here too, same as the ingest
        # recording sites, so a broken invariant just skips recording instead
        # of an AttributeError after `upsert_classification` already ran.
        if routing.mode == "user" and routing.credential is not None:
            acc.record(
                "classification",
                routing.credential.provider,
                attempt.usage,
                provider_call_succeeded=attempt.provider_call_succeeded,
            )
            usage_pending = True
        pending.append((message_id, label, confidence, rationale, model_version))
        if len(pending) >= batch_size:
            flush_pending()
    if pending:
        flush_pending()

    result = {
        "status": "ok",
        "created": created,
        "scanned": scanned,
        "skipped_user_overrides": skipped_user_overrides,
        "llm_attempted": llm_attempted,
        "llm_failed": llm_failed,
        "fell_back": fell_back,
        "failure_categories": failure_categories,
    }
    if include_task_counts:
        result["task_created"] = task_created
        result["task_processed"] = len(to_classify)
    return result
