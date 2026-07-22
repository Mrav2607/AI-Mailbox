"""Provider-agnostic sync dispatch: which Celery task a run enqueues, and
which accounts the scheduler considers eligible in the first place.

start_sync_run's task selection is exercised the same way test_sync_runs.py
already covers gmail dispatch: monkeypatch sys.modules['app.workers.tasks_ingest']
with a stand-in module so `import app.workers.tasks_ingest` (used inside
start_sync_run to dodge the circular import) picks it up without needing a
real Celery broker.

The two eligibility tests below are frozen regressions: outlook's scheduling
gate must never grow gmail's cursor-or-thread requirement, since a brand-new
outlook connection is cursor-null by construction (the first run establishes
the baseline) and a first walk over an empty mailbox never creates a thread.
"""

import sys
from unittest.mock import MagicMock
from uuid import uuid4

from app.services import sync_runs
from app.workers import tasks_ingest


_GMAIL_OPTIONS = {
    "max_results": 25,
    "skip_existing": True,
    "classify_messages": True,
    "new_only": False,
}


def _start_run(monkeypatch, *, task_module):
    monkeypatch.setattr(sync_runs, "active_sync", lambda *_a, **_k: None)
    monkeypatch.setitem(sys.modules, "app.workers.tasks_ingest", task_module)
    run = MagicMock(id=uuid4(), status="queued", error=None, completed_at=None)
    db = MagicMock()
    monkeypatch.setattr(sync_runs, "MailSyncRun", lambda **kwargs: run)
    return db, run


def test_ingest_tasks_maps_both_providers_to_their_task_name():
    assert sync_runs.INGEST_TASKS["gmail"] == "ingest_gmail_for_user"
    assert sync_runs.INGEST_TASKS["outlook"] == "ingest_outlook_for_user"


def test_start_sync_run_dispatches_gmail_accounts_to_the_gmail_task(monkeypatch):
    task_module = MagicMock()
    delay = MagicMock(return_value=MagicMock(id="task-gmail"))
    task_module.ingest_gmail_for_user.delay = delay
    db, run = _start_run(monkeypatch, task_module=task_module)

    result_run, deduplicated = sync_runs.start_sync_run(
        db, uuid4(), uuid4(), mode="manual", options=_GMAIL_OPTIONS, provider="gmail"
    )

    assert deduplicated is False
    assert result_run is run
    delay.assert_called_once()
    task_module.ingest_outlook_for_user.delay.assert_not_called()
    assert delay.call_args.kwargs["skip_existing"] is True
    assert delay.call_args.kwargs["new_only"] is False


def test_start_sync_run_dispatches_outlook_accounts_to_the_outlook_task(monkeypatch):
    task_module = MagicMock()
    delay = MagicMock(return_value=MagicMock(id="task-outlook"))
    task_module.ingest_outlook_for_user.delay = delay
    db, run = _start_run(monkeypatch, task_module=task_module)

    result_run, deduplicated = sync_runs.start_sync_run(
        db,
        uuid4(),
        uuid4(),
        mode="scheduled",
        # The generic options dict is gmail-shaped (skip_existing/new_only
        # included) even for an outlook account -- start_sync_run has to drop
        # what the outlook task doesn't accept, not the caller.
        options={
            "max_results": 50,
            "skip_existing": True,
            "classify_messages": True,
            "new_only": True,
        },
        provider="outlook",
    )

    assert deduplicated is False
    assert result_run is run
    task_module.ingest_gmail_for_user.delay.assert_not_called()
    delay.assert_called_once()
    kwargs = delay.call_args.kwargs
    assert kwargs["max_results"] == 50
    assert kwargs["classify_messages"] is True
    # A scheduled "new_only" tick collapses to outlook's normal bounded delta
    # run -- there's no new_only/skip_existing knob to forward.
    assert "new_only" not in kwargs
    assert "skip_existing" not in kwargs


def test_start_sync_run_rejects_an_unknown_provider_without_claiming_the_slot(
    monkeypatch,
):
    monkeypatch.setattr(sync_runs, "active_sync", lambda *_a, **_k: None)
    created = []
    monkeypatch.setattr(
        sync_runs, "MailSyncRun", lambda **kwargs: created.append(kwargs) or MagicMock()
    )
    db = MagicMock()

    try:
        sync_runs.start_sync_run(
            db, uuid4(), uuid4(), mode="manual", options=_GMAIL_OPTIONS, provider="yahoo"
        )
        assert False, "expected a ValueError"
    except ValueError:
        pass

    # Fails before ever building/committing a run row -- an unknown provider
    # must not leave a committed 'queued' row with nothing enqueued to
    # release its slot.
    assert created == []
    db.commit.assert_not_called()


# ---- frozen eligibility regressions ----


def test_fresh_cursor_null_outlook_account_is_scheduled():
    """A brand-new outlook connection has no delta cursor yet -- the first
    sync run establishes the baseline. Unlike gmail's history_id-or-thread
    gate, outlook eligibility must never wait on a cursor that doesn't exist
    yet, so its query carries no reference to outlook_delta_cursors at all.
    """
    from sqlalchemy.dialects import postgresql

    statements = []
    db = MagicMock()

    def execute(stmt):
        statements.append(
            str(
                stmt.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
        )
        return MagicMock(all=lambda: [])

    db.execute.side_effect = execute
    tasks_ingest._eligible_provider_rows(db)

    outlook_sql = [sql for sql in statements if "'outlook'" in sql]
    assert len(outlook_sql) == 1
    assert "outlook_delta_cursors" not in outlook_sql[0]
    assert "refresh_token IS NOT NULL" in outlook_sql[0]
    assert "sync_paused_at IS NULL" in outlook_sql[0]


def test_initialized_outlook_account_with_zero_threads_is_scheduled():
    """An outlook account that finished its first walk over an empty mailbox
    has zero mail_thread rows. Gmail's gate needs a cursor OR a thread so an
    empty-mailbox first ingest still counts -- outlook's query never
    joins/filters on mail_thread at all, so a zero-thread account is
    scheduled regardless of thread count.
    """
    from sqlalchemy.dialects import postgresql

    statements = []
    db = MagicMock()

    def execute(stmt):
        statements.append(
            str(
                stmt.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
        )
        return MagicMock(all=lambda: [])

    db.execute.side_effect = execute
    tasks_ingest._eligible_provider_rows(db)

    outlook_sql = [sql for sql in statements if "'outlook'" in sql]
    assert len(outlook_sql) == 1
    assert "mail_thread" not in outlook_sql[0]
    assert "EXISTS" not in outlook_sql[0]


def test_gmail_eligibility_keeps_its_cursor_or_thread_gate():
    """Asymmetry pin: outlook losing the cursor/thread gate must not silently
    take gmail's gate down with it."""
    from sqlalchemy.dialects import postgresql

    statements = []
    db = MagicMock()

    def execute(stmt):
        statements.append(
            str(
                stmt.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
        )
        return MagicMock(all=lambda: [])

    db.execute.side_effect = execute
    tasks_ingest._eligible_provider_rows(db)

    gmail_sql = [sql for sql in statements if "'gmail'" in sql]
    assert len(gmail_sql) == 1
    assert "gmail_history_id" in gmail_sql[0]
    assert "EXISTS" in gmail_sql[0]
