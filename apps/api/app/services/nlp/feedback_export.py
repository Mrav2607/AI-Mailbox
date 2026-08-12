"""Feedback export: the latest classification_feedback event per message, as
JSONL for the ml-side training/calibration tooling (plan
docs/plans/2026-08-11-feedback-capture-plan.md §3.4).

This is the service module; the runnable command is
`app/scripts/export_feedback.py`, frozen as:

    docker compose exec -T worker \
      python -m app.scripts.export_feedback > data/feedback-YYYYMMDD.jsonl

stdout purity is the whole point of the split: this module never touches
stdout/stderr directly except through the ``out``/``err`` parameters callers
pass in, so tests can capture both without patching sys.stdout.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import IO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ClassificationFeedback


@dataclass(frozen=True)
class ExportCounts:
    """Row accounting for one export run -- reported on stderr, never stdout."""

    written: int
    skipped_empty_text: int


def _latest_feedback_statement(*, user_id: UUID | None):
    """The `Select` for "the latest `classification_feedback` row per
    `message_id`" -- kept separate from `_latest_feedback_rows` so a test can
    `EXPLAIN` the exact statement production code runs, rather than an
    approximation of it.

    `DISTINCT ON (message_id) ... ORDER BY message_id, capture_seq DESC` is
    exactly the shape `ix_classification_feedback_msg_seq` (plan §3.1) was
    declared to serve -- Postgres can walk the index directly instead of
    sorting. `capture_seq`, not `created_at`, is the ordering key: Postgres
    `now()` is transaction-start time, not commit order, so `created_at` can
    mis-sort under concurrency.

    ``user_id`` narrows to one operator's corrections when given; omitted
    (the default), every message across every account is exported -- v1 is a
    single-operator tool.
    """
    stmt = (
        select(ClassificationFeedback)
        .distinct(ClassificationFeedback.message_id)
        .order_by(
            ClassificationFeedback.message_id,
            ClassificationFeedback.capture_seq.desc(),
        )
    )
    if user_id is not None:
        stmt = stmt.where(ClassificationFeedback.user_id == user_id)
    return stmt


def _latest_feedback_rows(db: Session, *, user_id: UUID | None):
    """Execute `_latest_feedback_statement` and return its scalar rows."""
    return db.execute(_latest_feedback_statement(user_id=user_id)).scalars()


def _serialize(row: ClassificationFeedback) -> dict:
    """One JSONL object per plan §3.4's frozen contract.

    ``text``/``label`` name the pair the way `ml/fit_calibration.py` and
    friends already consume it; the rest of the columns ride along as extra
    keys, which is a superset those readers already ignore.
    """
    return {
        "text": row.input_text,
        "label": row.new_label,
        "message_id": str(row.message_id),
        "user_id": str(row.user_id),
        "prior_label": row.prior_label,
        "prior_confidence": row.prior_confidence,
        "prior_model_version": row.prior_model_version,
        "source": row.source,
        "capture_seq": row.capture_seq,
        "created_at": row.created_at.isoformat(),
    }


def export_feedback_jsonl(
    db: Session,
    *,
    user_id: UUID | None = None,
    out: IO[str] = sys.stdout,
    err: IO[str] = sys.stderr,
) -> ExportCounts:
    """Write the latest feedback event per message to ``out`` as JSONL.

    ``out`` receives JSONL ONLY -- one `json.dumps(...)` object per line, no
    trailing summary. Every diagnostic (rows written, empty-text skips) goes
    to ``err`` instead, so `docker compose exec -T worker python -m
    app.scripts.export_feedback > file.jsonl` never contaminates the file
    with log noise.

    A row whose `input_text` is empty is skipped and counted rather than
    raising -- a single bad snapshot must not abort the whole export. Does
    not commit or otherwise mutate ``db``; this is a read-only export.
    """
    written = 0
    skipped_empty_text = 0
    for row in _latest_feedback_rows(db, user_id=user_id):
        if not row.input_text:
            skipped_empty_text += 1
            continue
        out.write(json.dumps(_serialize(row)))
        out.write("\n")
        written += 1

    err.write(
        f"feedback export: wrote {written} row(s), "
        f"skipped {skipped_empty_text} empty-text row(s)\n"
    )
    return ExportCounts(written=written, skipped_empty_text=skipped_empty_text)
