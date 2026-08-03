"""In-memory usage counters for per-user LLM usage visibility, flushed as one
additive multi-row UPSERT per batch
(see docs/plans/2026-08-02-llm-usage-visibility-plan.md §5).

`UsageAccumulator` buffers `record()` calls in memory across a run (a Gmail
sync, a backfill sweep, a single classify_message task) and is `flush()`ed at
each of that run's existing commit points. It never touches the DB inside
`record()` -- callers flush explicitly, and can `discard()` a batch that
belonged to a unit of work which itself rolled back, so a dead batch never
gets double-counted into the next flush.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import LlmUsageDaily
from app.services.nlp.llm_client import LlmUsage

# (user_id, usage_date, stage, provider) -- the same composite key as the
# table's unique constraint.
_CounterKey = tuple[UUID, date, str, str]


def _zero_row() -> dict[str, int]:
    return {
        "calls": 0,
        "calls_with_total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def _safe_nonneg_int(value: object) -> int | None:
    """Providers aren't trusted: a non-int or negative token count is treated
    as absent rather than let through to corrupt a sum. `bool` is excluded
    even though it's technically an `int` subclass -- a stray `True`/`False`
    must never silently become 1/0 tokens.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < 0:
        return None
    return value


class UsageAccumulator:
    """Buffers usage counters for one user across a run. Nothing here touches
    the DB until `flush()`.
    """

    def __init__(self, user_id: UUID) -> None:
        self._user_id = user_id
        self._batch: dict[_CounterKey, dict[str, int]] = {}

    def record(
        self,
        stage: str,
        provider: str,
        usage: LlmUsage | None,
        *,
        provider_call_succeeded: bool,
    ) -> None:
        """Add one attempt's counts to the in-memory batch.

        `calls` only increments when `provider_call_succeeded` -- heuristic
        or local classification, and calls that errored before the wire,
        never touch this counter. `usage_date` is stamped from the wall
        clock at record time (not batch-flush time), so a long run that
        crosses midnight correctly splits its counts across two rows instead
        of misdating the tail onto the day it happened to flush.

        Token fields are each independently optional -- a response can carry
        `prompt_tokens` and omit `total_tokens` entirely.
        `calls_with_total_tokens` and the `total_tokens` sum are gated
        specifically on a usable `total_tokens` field, since that's the one
        number the UI presents as an honest total; it is never synthesized
        from prompt + completion when the provider left it out.
        `prompt_tokens` / `completion_tokens` sum independently of that gate,
        whenever each is itself present and valid.
        """
        if not provider_call_succeeded:
            return

        key = (self._user_id, datetime.now(UTC).date(), stage, provider)
        row = self._batch.setdefault(key, _zero_row())
        row["calls"] += 1

        if usage is None:
            return

        total = _safe_nonneg_int(usage.total_tokens)
        if total is not None:
            row["calls_with_total_tokens"] += 1
            row["total_tokens"] += total

        prompt = _safe_nonneg_int(usage.prompt_tokens)
        if prompt is not None:
            row["prompt_tokens"] += prompt

        completion = _safe_nonneg_int(usage.completion_tokens)
        if completion is not None:
            row["completion_tokens"] += completion

    def flush(self, db: Session) -> None:
        """Emit one multi-row UPSERT for everything buffered so far. Does not
        commit -- the caller's existing commit lands it.

        Rows go in sorted by the full composite key before the statement is
        built. That's not cosmetic: it gives every worker the same lock
        order, so two workers flushing overlapping keys can't deadlock each
        waiting on a row the other already locked.

        The UPSERT is additive (`calls = llm_usage_daily.calls +
        EXCLUDED.calls`, same shape for every counter), so two concurrent
        flushes touching the same key compose into the right total instead
        of one clobbering the other's increment.
        """
        if not self._batch:
            return

        rows = [
            {
                "user_id": key[0],
                "usage_date": key[1],
                "stage": key[2],
                "provider": key[3],
                **counters,
            }
            for key, counters in sorted(self._batch.items())
        ]

        stmt = insert(LlmUsageDaily).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_llm_usage_daily_key",
            set_={
                "calls": LlmUsageDaily.calls + stmt.excluded.calls,
                "calls_with_total_tokens": (
                    LlmUsageDaily.calls_with_total_tokens
                    + stmt.excluded.calls_with_total_tokens
                ),
                "prompt_tokens": (
                    LlmUsageDaily.prompt_tokens + stmt.excluded.prompt_tokens
                ),
                "completion_tokens": (
                    LlmUsageDaily.completion_tokens + stmt.excluded.completion_tokens
                ),
                "total_tokens": (
                    LlmUsageDaily.total_tokens + stmt.excluded.total_tokens
                ),
                "updated_at": text("now()"),
            },
        )
        db.execute(stmt)

    def discard(self) -> None:
        """Drop the buffered batch without writing -- used when a flush (or
        the surrounding unit of work) rolled back, so that dead batch can
        never be double-counted into the next flush.
        """
        self._batch = {}

    def committed(self) -> None:
        """Clear the buffer after the caller's commit has actually returned.
        Distinct from `discard()` in intent even though the effect is the
        same: this is the happy path, only called once the write is durable.
        """
        self._batch = {}
