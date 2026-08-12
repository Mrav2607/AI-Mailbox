"""Tests for feedback_export.py and its entrypoint, app/scripts/export_feedback.py
(plan docs/plans/2026-08-11-feedback-capture-plan.md §3.4).

`export_feedback_jsonl` is driven with a lightweight fake session here --
latest-per-message resolution is the DB query's job (`DISTINCT ON`), and a
fake can't prove real SQL semantics honestly; that proof lives in
test_feedback_integration.py's real-Postgres tier. This file covers the part
that IS a fake session's job: the JSONL/stdout contract itself (one object
per row, empty-text rows skipped and counted on stderr, stdout never
contaminated with anything else) plus the entrypoint's arg parsing and its
promise to never wire up the app's stdout logging config.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.services.nlp import feedback_export
from app.services.nlp.feedback_export import export_feedback_jsonl


def _row(
    *,
    message_id=None,
    user_id=None,
    input_text="hello",
    new_label="fyi",
    prior_label=None,
    prior_confidence=None,
    prior_model_version=None,
    source="reclassify",
    capture_seq=1,
    created_at=None,
):
    return SimpleNamespace(
        message_id=message_id or uuid4(),
        user_id=user_id or uuid4(),
        input_text=input_text,
        new_label=new_label,
        prior_label=prior_label,
        prior_confidence=prior_confidence,
        prior_model_version=prior_model_version,
        source=source,
        capture_seq=capture_seq,
        created_at=created_at or datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc),
    )


class _FakeDB:
    """Stands in for the Session -- `_latest_feedback_rows` only ever calls
    `db.execute(stmt).scalars()` and iterates the result, so a canned row
    list is all this needs to answer."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, stmt):
        return SimpleNamespace(scalars=lambda: iter(self._rows))


# ---------------------------------------------------------------------------
# export_feedback_jsonl: the JSONL/stdout contract
# ---------------------------------------------------------------------------


def test_jsonl_parses_with_newlines_and_non_ascii_in_input_text():
    tricky_text = "line one\nline two\ncafé — déjà vu — 日本語"
    db = _FakeDB([_row(input_text=tricky_text, new_label="needs_reply")])
    out = io.StringIO()
    err = io.StringIO()

    counts = export_feedback_jsonl(db, out=out, err=err)

    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["text"] == tricky_text
    assert parsed["label"] == "needs_reply"
    assert counts.written == 1
    assert counts.skipped_empty_text == 0


def test_output_shape_matches_the_frozen_contract():
    message_id = uuid4()
    user_id = uuid4()
    row = _row(
        message_id=message_id,
        user_id=user_id,
        input_text="hi there",
        new_label="action_required",
        prior_label="fyi",
        prior_confidence=0.42,
        prior_model_version="heuristic-v1",
        source="reclassify",
        capture_seq=7,
        created_at=datetime(2026, 8, 11, 9, 30, 0, tzinfo=timezone.utc),
    )
    db = _FakeDB([row])
    out = io.StringIO()

    export_feedback_jsonl(db, out=out, err=io.StringIO())

    parsed = json.loads(out.getvalue().splitlines()[0])
    assert parsed == {
        "text": "hi there",
        "label": "action_required",
        "message_id": str(message_id),
        "user_id": str(user_id),
        "prior_label": "fyi",
        "prior_confidence": 0.42,
        "prior_model_version": "heuristic-v1",
        "source": "reclassify",
        "capture_seq": 7,
        "created_at": "2026-08-11T09:30:00+00:00",
    }


def test_stdout_purity_no_stray_lines_are_not_valid_jsonl():
    db = _FakeDB(
        [_row(new_label="fyi"), _row(input_text="", new_label="spam"), _row(new_label="promotional")]
    )
    out = io.StringIO()

    export_feedback_jsonl(db, out=out, err=io.StringIO())

    lines = out.getvalue().splitlines()
    # Every single line must parse as its own JSON object -- one skipped
    # (empty-text) row must not leave a blank line or partial write behind.
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


def test_empty_text_rows_are_skipped_and_counted_on_stderr():
    db = _FakeDB(
        [
            _row(input_text="", new_label="fyi"),
            _row(input_text="real content", new_label="spam"),
            _row(input_text="", new_label="promotional"),
        ]
    )
    out = io.StringIO()
    err = io.StringIO()

    counts = export_feedback_jsonl(db, out=out, err=err)

    assert counts.written == 1
    assert counts.skipped_empty_text == 2
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["label"] == "spam"
    # The skip count is reported, but only on stderr -- never stdout.
    assert "2" in err.getvalue()
    assert err.getvalue() != ""


def test_export_writes_one_line_per_row_the_query_handed_it():
    # Complements test_feedback_integration.py's real-Postgres DISTINCT ON
    # proof: given rows the query has ALREADY resolved to one per message,
    # export must pass every one of them through untouched -- no accidental
    # de-dup, no dropped rows.
    rows = [_row(new_label="fyi"), _row(new_label="spam"), _row(new_label="needs_reply")]
    db = _FakeDB(rows)
    out = io.StringIO()

    counts = export_feedback_jsonl(db, out=out, err=io.StringIO())

    assert counts.written == 3
    assert len(out.getvalue().splitlines()) == 3


def test_user_filter_narrows_the_statement_to_that_user():
    user_id = uuid4()
    captured_stmt = {}

    class _CapturingDB:
        def execute(self, stmt):
            captured_stmt["stmt"] = stmt
            return SimpleNamespace(scalars=lambda: iter([]))

    export_feedback_jsonl(_CapturingDB(), user_id=user_id, out=io.StringIO(), err=io.StringIO())

    # The SELECT lists user_id as a column either way -- only a WHERE
    # predicate with the bound uuid proves the filter actually narrows.
    compiled = captured_stmt["stmt"].compile(dialect=postgresql.dialect())
    assert "where classification_feedback.user_id =" in str(compiled).lower()
    assert user_id in compiled.params.values()


# ---------------------------------------------------------------------------
# app/scripts/export_feedback.py: the entrypoint
# ---------------------------------------------------------------------------


class _FakeSessionContext:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *exc_info):
        return False


def test_main_forwards_the_user_filter_and_uses_a_fresh_session(monkeypatch):
    from app.scripts import export_feedback as script

    fake_db = object()
    monkeypatch.setattr(script, "SessionLocal", lambda: _FakeSessionContext(fake_db))

    calls = {}

    def fake_export(db, *, user_id, out, err):
        calls["db"] = db
        calls["user_id"] = user_id
        return feedback_export.ExportCounts(written=0, skipped_empty_text=0)

    monkeypatch.setattr(script, "export_feedback_jsonl", fake_export)

    user_id = uuid4()
    exit_code = script.main(["--user", str(user_id)])

    assert exit_code == 0
    assert calls == {"db": fake_db, "user_id": user_id}


def test_main_defaults_to_no_user_filter(monkeypatch):
    from app.scripts import export_feedback as script

    fake_db = object()
    monkeypatch.setattr(script, "SessionLocal", lambda: _FakeSessionContext(fake_db))

    calls = {}

    def fake_export(db, *, user_id, out, err):
        calls["user_id"] = user_id
        return feedback_export.ExportCounts(written=0, skipped_empty_text=0)

    monkeypatch.setattr(script, "export_feedback_jsonl", fake_export)

    script.main([])

    assert calls["user_id"] is None


def test_script_never_wires_up_the_apps_stdout_logging_config():
    # configure_logging() hangs a handler on the root logger that writes JSON
    # lines to stdout -- calling it here would interleave log noise into the
    # JSONL this script promises on stdout. Checked at the AST level (import
    # statements only) so the module's own docstring explaining this rule
    # doesn't trip the guard it's describing.
    from app.scripts import export_feedback as script

    tree = ast.parse(inspect.getsource(script))
    imported_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "configure_logging" not in imported_names
    assert not hasattr(script, "configure_logging")
