"""Tests for `UsageAccumulator` -- the in-memory usage-counter batch behind
per-user LLM usage visibility (plan §5). Deterministic and offline: no real
DB, no network. `flush()` is exercised by inspecting the compiled UPSERT
statement's params/SET clause, mirroring this repo's compiled-SQL-inspection
style (see test_extraction_run.py, test_llm_settings.py) rather than
executing against a live database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.services.nlp.usage import UsageAccumulator

USER_ID = uuid4()


@dataclass(frozen=True)
class _Usage:
    """Local stand-in for the real `LlmUsage` dataclass in llm_client.py --
    kept separate (rather than importing the real one) so these tests can
    hand `UsageAccumulator` hostile values (strings, bools, negatives) that
    the real frozen dataclass wouldn't naturally carry. `UsageAccumulator`
    only relies on the three attribute names, so the stand-in works either way.
    """

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


def _compiled(stmt):
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _fake_db():
    """Stands in for a Session: flush() only ever calls db.execute()."""
    db = MagicMock()
    return db


def test_provider_call_failed_records_nothing():
    acc = UsageAccumulator(USER_ID)
    acc.record("classification", "openai", _Usage(10, 5, 15), provider_call_succeeded=False)

    db = _fake_db()
    acc.flush(db)
    db.execute.assert_not_called()


def test_usage_none_still_counts_a_call_but_not_tokens():
    acc = UsageAccumulator(USER_ID)
    acc.record("classification", "openai", None, provider_call_succeeded=True)

    db = _fake_db()
    acc.flush(db)
    stmt = db.execute.call_args[0][0]
    sql = _compiled(stmt)
    assert "calls" in sql
    # A single successful call, zero token counters.
    assert stmt.compile().params["calls_m0"] == 1
    assert stmt.compile().params["calls_with_total_tokens_m0"] == 0
    assert stmt.compile().params["total_tokens_m0"] == 0


def test_partial_usage_prompt_only_does_not_fabricate_a_total():
    acc = UsageAccumulator(USER_ID)
    acc.record(
        "extraction",
        "gemini",
        _Usage(prompt_tokens=42, completion_tokens=None, total_tokens=None),
        provider_call_succeeded=True,
    )

    db = _fake_db()
    acc.flush(db)
    params = db.execute.call_args[0][0].compile().params

    assert params["calls_m0"] == 1
    # No usable total_tokens came back -- calls_with_total_tokens must stay 0
    # and total_tokens must not be synthesized from prompt + completion.
    assert params["calls_with_total_tokens_m0"] == 0
    assert params["total_tokens_m0"] == 0
    # prompt_tokens is independently present and gets summed on its own.
    assert params["prompt_tokens_m0"] == 42
    assert params["completion_tokens_m0"] == 0


def test_full_usage_increments_calls_with_total_tokens_and_sums():
    acc = UsageAccumulator(USER_ID)
    acc.record(
        "classification",
        "openai",
        _Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        provider_call_succeeded=True,
    )

    db = _fake_db()
    acc.flush(db)
    params = db.execute.call_args[0][0].compile().params

    assert params["calls_m0"] == 1
    assert params["calls_with_total_tokens_m0"] == 1
    assert params["prompt_tokens_m0"] == 10
    assert params["completion_tokens_m0"] == 5
    assert params["total_tokens_m0"] == 15


def test_negative_and_non_int_tokens_are_ignored():
    acc = UsageAccumulator(USER_ID)
    acc.record(
        "classification",
        "openai",
        _Usage(prompt_tokens=-5, completion_tokens="bogus", total_tokens=True),
        provider_call_succeeded=True,
    )

    db = _fake_db()
    acc.flush(db)
    params = db.execute.call_args[0][0].compile().params

    assert params["calls_m0"] == 1
    # -5 is negative, "bogus" isn't an int, and True is a bool (excluded even
    # though bool subclasses int) -- all three must be dropped, not counted.
    assert params["prompt_tokens_m0"] == 0
    assert params["completion_tokens_m0"] == 0
    assert params["total_tokens_m0"] == 0
    assert params["calls_with_total_tokens_m0"] == 0


def test_two_records_same_key_merge_into_one_row():
    acc = UsageAccumulator(USER_ID)
    acc.record("classification", "openai", _Usage(10, 5, 15), provider_call_succeeded=True)
    acc.record("classification", "openai", _Usage(20, 10, 30), provider_call_succeeded=True)

    db = _fake_db()
    acc.flush(db)
    stmt = db.execute.call_args[0][0]
    params = stmt.compile().params

    # Merged in-memory before the statement is even built -- one row, not two.
    assert params["calls_m0"] == 2
    assert params["calls_with_total_tokens_m0"] == 2
    assert params["prompt_tokens_m0"] == 30
    assert params["completion_tokens_m0"] == 15
    assert params["total_tokens_m0"] == 45
    assert "calls_m1" not in params


def test_different_stage_and_provider_stay_separate_rows():
    acc = UsageAccumulator(USER_ID)
    acc.record("classification", "openai", _Usage(1, 1, 2), provider_call_succeeded=True)
    acc.record("extraction", "openai", _Usage(1, 1, 2), provider_call_succeeded=True)
    acc.record("classification", "gemini", _Usage(1, 1, 2), provider_call_succeeded=True)

    db = _fake_db()
    acc.flush(db)
    params = db.execute.call_args[0][0].compile().params

    assert params["calls_m0"] == 1
    assert params["calls_m1"] == 1
    assert params["calls_m2"] == 1
    assert "calls_m3" not in params


def test_rows_are_sorted_by_composite_key_for_deadlock_avoidance():
    """The flush must emit rows in composite-key order regardless of
    insertion order -- that's what lets two workers flushing overlapping
    keys take Postgres row locks in the same order and never deadlock.
    """
    acc = UsageAccumulator(USER_ID)
    # Insert "z"-ish provider first, "a"-ish provider second -- out of order.
    acc.record("extraction", "openrouter", None, provider_call_succeeded=True)
    acc.record("classification", "custom", None, provider_call_succeeded=True)
    acc.record("classification", "groq", None, provider_call_succeeded=True)

    db = _fake_db()
    acc.flush(db)
    stmt = db.execute.call_args[0][0]
    compiled = stmt.compile(dialect=postgresql.dialect())
    params = compiled.params
    ordered_keys = [
        (params[f"stage_m{i}"], params[f"provider_m{i}"]) for i in range(3)
    ]
    assert ordered_keys == sorted(ordered_keys)
    assert ordered_keys == [
        ("classification", "custom"),
        ("classification", "groq"),
        ("extraction", "openrouter"),
    ]


def test_upsert_is_additive_not_replace():
    acc = UsageAccumulator(USER_ID)
    acc.record("classification", "openai", _Usage(1, 1, 2), provider_call_succeeded=True)

    db = _fake_db()
    acc.flush(db)
    sql = _compiled(db.execute.call_args[0][0])

    assert "ON CONFLICT" in sql.upper()
    # Additive SET, not a plain overwrite -- concurrent flushes for the same
    # key must compose instead of one clobbering the other's increment.
    assert "llm_usage_daily.calls +" in sql or "llm_usage_daily.calls+" in sql
    assert "llm_usage_daily.total_tokens +" in sql or "llm_usage_daily.total_tokens+" in sql


def test_discard_then_flush_writes_nothing():
    acc = UsageAccumulator(USER_ID)
    acc.record("classification", "openai", _Usage(1, 1, 2), provider_call_succeeded=True)
    acc.discard()

    db = _fake_db()
    acc.flush(db)
    db.execute.assert_not_called()


def test_committed_clears_the_buffer():
    acc = UsageAccumulator(USER_ID)
    acc.record("classification", "openai", _Usage(1, 1, 2), provider_call_succeeded=True)
    acc.committed()

    db = _fake_db()
    acc.flush(db)
    db.execute.assert_not_called()


def test_crossing_midnight_produces_two_rows(monkeypatch):
    """A run that crosses midnight must split its counts into two rows, not
    misdate everything onto the day the batch happened to flush.
    """
    clock = iter(
        [
            SimpleNamespace(date=lambda: date(2026, 7, 31)),
            SimpleNamespace(date=lambda: date(2026, 8, 1)),
        ]
    )

    class _FakeDatetime:
        @staticmethod
        def now(_tz):
            return next(clock)

    monkeypatch.setattr("app.services.nlp.usage.datetime", _FakeDatetime)

    acc = UsageAccumulator(USER_ID)
    acc.record("classification", "openai", _Usage(1, 1, 2), provider_call_succeeded=True)
    acc.record("classification", "openai", _Usage(1, 1, 2), provider_call_succeeded=True)

    db = _fake_db()
    acc.flush(db)
    params = db.execute.call_args[0][0].compile().params

    usage_dates = {params["usage_date_m0"], params["usage_date_m1"]}
    assert usage_dates == {date(2026, 7, 31), date(2026, 8, 1)}
    assert params["calls_m0"] == 1
    assert params["calls_m1"] == 1
