from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from .celery_app import celery_app
from app.core.logging import logger
from app.db.base import SessionLocal
from app.db.models import LlmUsageDaily, MailThread, MailMessage
from app.services.nlp.backfill import run_backfill
from app.services.nlp.classifier import classify_with_usage, build_classification_text
from app.services.nlp.extraction_run import (
    run_extraction_for_message,
    run_extraction_sweep,
    terminalize_expired_pending,
    users_with_claimable_action_items,
    users_with_unclaimed_actionable_messages,
)
from app.services.nlp.llm_client import WORKER_RETRIES
from app.services.nlp.persistence import upsert_classification
from app.services.nlp.providers import extraction_feature_enabled, resolve_classification_routing
from app.services.nlp.usage import UsageAccumulator

# Sweep cap for each recovery-tick pass -- generous enough to drain a normal
# backlog in one tick, cheap enough that a tick never runs long even for a
# user with a lot of retryable work.
_RECOVERY_SWEEP_LIMIT = 25

# 400 days, not the usual 90 -- a user comparing this August to last August
# needs both Augusts still on the table. Don't "tidy" this down to 90; that
# would quietly break year-over-year usage comparisons (plan §8).
_USAGE_RETENTION = timedelta(days=400)

# extract_actions_for_user's own soft time limit (the decorator below) --
# named so the deadline handed to run_extraction_sweep can be computed FROM
# it instead of duplicating the same number as an independent magic constant
# that could silently drift out of sync with the decorator.
_EXTRACT_ACTIONS_SOFT_TIME_LIMIT_S = 1740.0
# Margin under that soft limit for run_extraction_sweep's own deadline check
# to trip BEFORE Celery's signal does (plan: phase 3 of the LLM-failure
# work, Codex review blocker: per-message retry budgets alone don't bound a
# sweep where every call keeps succeeding) -- gives the task time to finish
# its current commit and return a clean partial result instead of racing an
# asynchronously-delivered SoftTimeLimitExceeded that could land mid-write.
_SWEEP_DEADLINE_MARGIN_S = 120.0

# extraction_recovery_tick's own soft time limit and margin -- much smaller
# than the dedicated sweep task's, since a tick shares its whole budget
# across TWO sweep passes (row-driven and message-driven) plus its own
# candidate-selection queries.
_RECOVERY_TICK_SOFT_TIME_LIMIT_S = 270.0
_RECOVERY_TICK_DEADLINE_MARGIN_S = 20.0


@celery_app.task
def classify_message(message_id: str) -> dict:
    with SessionLocal() as db:
        message = db.get(MailMessage, UUID(message_id))
        if not message:
            return {"message_id": message_id, "status": "missing"}
        thread = db.get(MailThread, message.thread_id)
        text_for_classification = build_classification_text(
            thread.subject if thread else None,
            message.snippet,
            message.body_text,
        )
        # A single message, so no per-run router (and its memo) is needed --
        # resolve once, directly.
        routing = resolve_classification_routing(db, thread.user_id) if thread else None
        # Celery task -- nobody's watching a spinner for this one.
        attempt = classify_with_usage(text_for_classification, routing=routing, policy=WORKER_RETRIES)
        # Phase 2 (D-C): `verdict is None` means the BYOK call failed and the
        # user hasn't opted into local fallback (or the encoder couldn't
        # serve either) -- skip the write entirely so the message stays a
        # backfill candidate, never a stranded null-label row.
        verdict = attempt.verdict
        outcome = None
        label = confidence = None
        if verdict is not None:
            label, confidence, rationale, model_version = verdict
            outcome = upsert_classification(
                db,
                message_id=message.id,
                label=label,
                confidence=confidence,
                rationale=rationale,
                model_version=model_version,
            )

        # Only a resolved "user" payer gets recorded -- operator-paid usage
        # is an explicit v1 non-goal (plan §1), and a threadless message has
        # no user to attribute it to anyway. `routing.credential is not None`
        # guards the same broken invariant classifier.py's `_classify_llm`
        # already degrades for -- here it just means skip recording rather
        # than an AttributeError after `upsert_classification` already ran.
        usage_recorded = (
            thread is not None
            and routing is not None
            and routing.mode == "user"
            and routing.credential is not None
        )
        if usage_recorded:
            acc = UsageAccumulator(thread.user_id)
            acc.record(
                "classification",
                routing.credential.provider,
                attempt.usage,
                provider_call_succeeded=attempt.provider_call_succeeded,
            )

        # db.flush() sits OUTSIDE the try: begin_nested() flushes pending ORM
        # state before opening its SAVEPOINT, so a genuine business-write
        # failure would otherwise surface inside the usage handler with the
        # outer transaction already invalid (plan §5). Skip the block
        # entirely when nothing was recorded -- no point opening a SAVEPOINT
        # for an empty batch.
        db.flush()
        if usage_recorded:
            try:
                with db.begin_nested():
                    acc.flush(db)
            except SQLAlchemyError:
                logger.warning(
                    "usage flush failed for message %s; discarding batch",
                    message_id,
                )
                acc.discard()
        db.commit()
        if usage_recorded:
            acc.committed()
        # A "protected" outcome means a user override landed on this message
        # before this write reached it -- the label above never persisted, so
        # returning it here would misreport a classification that didn't
        # happen.
        if outcome == "protected":
            return {"message_id": message_id, "status": "skipped_user_override"}
        if verdict is None:
            return {"message_id": message_id, "status": "left_unclassified"}
        return {"message_id": message_id, "label": label, "confidence": confidence}


# Same shape as the ingest task's safeguards: a 500-thread Gemini backfill can
# run a long while, so the time limit is a hung-call backstop, and retries with
# backoff cover transient classifier/DB failures. Already-labeled messages are
# skipped on re-run (unless force), so a retry resumes rather than redoing the
# whole batch.
@celery_app.task(
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
    time_limit=1800,
    soft_time_limit=1740,
)
def backfill_threads_for_user(
    user_id: str,
    limit: int = 100,
    force: bool = False,
    bucket: str = "unclassified",
    backend: str | None = None,
) -> dict:
    """Run a classification backfill off the request path. The backfill route
    enqueues us for anything over its inline cap and returns 202; bucket and
    backend were already validated there."""
    with SessionLocal() as db:
        # Celery task -- always the WORKER_RETRIES side of run_backfill's
        # inline/worker split (the route handles the inline side directly).
        result = run_backfill(
            db,
            UUID(user_id),
            limit=limit,
            force=force,
            bucket=bucket,
            backend=backend,
            policy=WORKER_RETRIES,
        )
    return {"user_id": user_id, **result}


@celery_app.task
def classify_latest_threads(
    user_id: str, limit: int = 25, force: bool = False
) -> dict:
    """Classify the latest message in the user's most recent threads."""
    with SessionLocal() as db:
        # Celery task -- same WORKER_RETRIES reasoning as backfill_threads_for_user.
        result = run_backfill(
            db,
            UUID(user_id),
            limit=limit,
            force=force,
            bucket="all",
            include_task_counts=True,
            policy=WORKER_RETRIES,
        )
    # user_id rides along because non-sync tasks don't have a durable ownership
    # row for the task-status endpoint to consult.
    return {
        "status": result["status"],
        "user_id": user_id,
        "created": result["task_created"],
        "processed": result["task_processed"],
        "skipped_user_overrides": result["skipped_user_overrides"],
        # Failure-visibility counters (plan: 2026-08-14-llm-failure-visibility)
        # -- forwarded, not reshaped, same as backfill_threads_for_user's
        # **result. Dropping these here would let a queued run over this
        # route look like plain success even when the LLM failed.
        "llm_attempted": result["llm_attempted"],
        "llm_failed": result["llm_failed"],
        "fell_back": result["fell_back"],
        "failure_categories": result["failure_categories"],
        "left_unclassified": result["left_unclassified"],
    }


# Same autoretry shape as backfill_threads_for_user. It composes with the
# claim state machine: an exception between claim-commit and record-commit
# fences the in-flight claim to failed (claimable) before propagating, so a
# retry's sweep reclaims that row instead of skipping a live pending it owns.
@celery_app.task(
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
    time_limit=1800,
    soft_time_limit=1740,
)
def extract_action_for_message(message_id: str) -> dict:
    """Extract one message's action item -- the reclassify hook's path."""
    with SessionLocal() as db:
        result = run_extraction_for_message(db, UUID(message_id))
    return {"message_id": message_id, **result}


@celery_app.task(
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
    time_limit=1800,
    soft_time_limit=_EXTRACT_ACTIONS_SOFT_TIME_LIMIT_S,
)
def extract_actions_for_user(
    user_id: str, limit: int = 100, force: bool = False, since_days: int = 30
) -> dict:
    """Run an action-extraction sweep off the request path -- the backfill
    route enqueues us, and the ingest hook fires us after mail lands."""
    with SessionLocal() as db:
        # deadline: a proactive backstop against a sweep that keeps
        # succeeding (see run_extraction_sweep's own docstring) -- computed
        # from this task's own soft time limit, not a guess, so it can never
        # fire LATER than Celery's own signal would.
        result = run_extraction_sweep(
            db,
            UUID(user_id),
            limit=limit,
            force=force,
            since_days=since_days,
            deadline=time.monotonic()
            + _EXTRACT_ACTIONS_SOFT_TIME_LIMIT_S
            - _SWEEP_DEADLINE_MARGIN_S,
        )
    return {"user_id": user_id, **result}


@celery_app.task(ignore_result=True, time_limit=300, soft_time_limit=_RECOVERY_TICK_SOFT_TIME_LIMIT_S)
def extraction_recovery_tick() -> dict:
    """Beat-scheduled safety net (every 900s -- celery_app.py) so retryable
    rows don't depend on new mail arriving to get swept: a quiet sync, a
    zero-upsert run, or SCHEDULED_SYNC_INTERVAL_SECONDS=0 must not strand
    work. Cheap no-op unless the operator flag is on (`extraction_feature_
    enabled()` -- flag only, no DB; per-user credential coverage is checked
    below, inside each sweep); otherwise terminalizes every stuck-at-cap
    pending row (global, not just the users swept below -- a user with no
    OTHER claimable work would never appear in either set otherwise), then
    sweeps two independently derived user sets:

    - row-driven: users owning claimable action_item rows (failed/expired
      pending/reclassified-back ineligible), swept with recovery=True
      (row-driven, no since_days age filter);
    - message-driven: users with an actionable message that has NO
      action_item row at all -- a broker failure that swallowed the ingest
      hook's enqueue leaves no row behind, so the row-driven set alone can
      never see that user; this pass is what recovers them.

    Both user sets are derived independently of BYOK coverage on purpose
    (filtering them by credential would be an optimization, not a
    correctness requirement) -- a credential-less user's own sweep resolves
    that internally and returns a cheap ``disabled`` result without burning
    an attempt, so sweeping them here costs one resolve call and nothing
    else.

    One bad user's sweep must never abort the tick for the rest (same
    fan-out isolation as dispatch_scheduled_syncs): roll back and log, then
    move on. A soft time limit backstops the hard `time_limit=300` kill --
    a hard SIGKILL happens mid-claim with no chance to fence the row, so
    each kill would burn a claim-time attempt for nothing; the soft limit
    lets the tick stop cleanly instead and return partial counts, leaving
    whatever's left for the next tick 900s later.

    No autoretry -- the next tick is the retry.
    """
    if not extraction_feature_enabled():
        return {"status": "disabled"}

    # One deadline for the WHOLE tick (both passes below), not one per
    # run_extraction_sweep call -- a per-call deadline would let the
    # row-driven pass alone use the tick's entire budget and leave nothing
    # for the message-driven pass. Computed from this task's own soft time
    # limit, same "thread it, don't guess" reasoning as
    # extract_actions_for_user's deadline above.
    deadline = time.monotonic() + _RECOVERY_TICK_SOFT_TIME_LIMIT_S - _RECOVERY_TICK_DEADLINE_MARGIN_S

    row_driven_swept = 0
    message_driven_swept = 0
    try:
        with SessionLocal() as db:
            terminalize_expired_pending(db)

            row_driven_users = users_with_claimable_action_items(db)
            for user_id in row_driven_users:
                try:
                    run_extraction_sweep(
                        db, user_id, limit=_RECOVERY_SWEEP_LIMIT, recovery=True, deadline=deadline
                    )
                except SoftTimeLimitExceeded:
                    raise
                except Exception:
                    db.rollback()
                    logger.exception(
                        "action extraction recovery tick failed for "
                        "row-driven user %s",
                        user_id,
                    )
                    continue
                row_driven_swept += 1

            message_driven_users = users_with_unclaimed_actionable_messages(db)
            for user_id in message_driven_users:
                try:
                    run_extraction_sweep(
                        db, user_id, limit=_RECOVERY_SWEEP_LIMIT, deadline=deadline
                    )
                except SoftTimeLimitExceeded:
                    raise
                except Exception:
                    db.rollback()
                    logger.exception(
                        "action extraction recovery tick failed for "
                        "message-driven user %s",
                        user_id,
                    )
                    continue
                message_driven_swept += 1
    except SoftTimeLimitExceeded:
        logger.exception("action extraction recovery tick hit its soft time limit")
        return {
            "status": "timed_out",
            "row_driven_users": row_driven_swept,
            "message_driven_users": message_driven_swept,
        }

    return {
        "status": "ok",
        "row_driven_users": row_driven_swept,
        "message_driven_users": message_driven_swept,
    }


@celery_app.task(ignore_result=True, time_limit=300, soft_time_limit=270)
def prune_llm_usage_daily() -> dict:
    """Beat-scheduled retention sweep (daily -- celery_app.py) for
    `llm_usage_daily`. This is housekeeping, not a recovery tick, so it
    doesn't need extraction_recovery_tick's 900s cadence -- a day-old
    straggler row costs nothing, and a daily-granularity table doesn't need
    finer-grained pruning.

    Deletes on `usage_date < cutoff`, served by the standalone `usage_date`
    index (plan §4/§8) -- the unique constraint leads with `user_id` and
    can't serve this range scan.

    One bad run must never take down the beat worker: catch, roll back, log,
    and let tomorrow's tick retry. No autoretry -- same reasoning as
    extraction_recovery_tick, the next scheduled run is the retry.
    """
    cutoff = (datetime.now(timezone.utc) - _USAGE_RETENTION).date()
    try:
        with SessionLocal() as db:
            result = db.execute(
                delete(LlmUsageDaily).where(LlmUsageDaily.usage_date < cutoff)
            )
            deleted = result.rowcount or 0
            db.commit()
    except SQLAlchemyError:
        logger.exception("llm_usage_daily retention sweep failed")
        return {"status": "error"}

    logger.info("llm_usage_daily retention sweep deleted %d row(s)", deleted)
    return {"status": "ok", "deleted": deleted}
