"""Entrypoint for the classification-feedback export (plan
docs/plans/2026-08-11-feedback-capture-plan.md §3.4).

The worker image ships `/app/app` only -- no `data/`, no `ml/`, no writable
host mount -- so this is run worker-exec, with the host redirecting stdout to
its own `data/` directory. This exact command is the frozen contract:

    docker compose exec -T worker \
      python -m app.scripts.export_feedback > data/feedback-YYYYMMDD.jsonl

Deliberately does NOT call `app.core.logging.configure_logging()` -- that
wires a handler onto the root logger that writes JSON lines to stdout, which
would interleave log noise into the JSONL this script promises on stdout.
Diagnostics here go straight to stderr instead.

Global by default (single operator, multiple connected accounts); pass
`--user <uuid>` to scope to one user's corrections.
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

from app.db.base import SessionLocal
from app.services.nlp.feedback_export import export_feedback_jsonl


def main(argv: list[str] | None = None) -> int:
    """Parse args, run the export against a fresh DB session, and exit 0.

    ``argv`` defaults to `sys.argv[1:]` (argparse's own default) -- the
    parameter exists so tests can drive this without a subprocess.
    """
    parser = argparse.ArgumentParser(
        description="Export the latest classification_feedback event per "
        "message as JSONL to stdout."
    )
    parser.add_argument(
        "--user",
        type=UUID,
        default=None,
        help="Restrict the export to one user's corrections (default: every user).",
    )
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        export_feedback_jsonl(db, user_id=args.user, out=sys.stdout, err=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
