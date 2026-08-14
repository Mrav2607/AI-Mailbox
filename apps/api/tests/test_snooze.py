"""Snooze feature tests (docs/plans/2026-08-13-snooze-plan.md):

- the shared active-snooze predicate and its snoozed_until projection, at
  the SQL-shape level (this suite is offline, so these are compiled-
  statement assertions, same style as test_mailbox_pagination.py);
- triage/counts bucket filtering and the snoozed-bucket sort contract;
- the snooze/unsnooze/re-snooze endpoint, its validation bounds, and the
  done/snooze exclusivity race (§3.3) in both orderings;
- exposure of the projected snoozed_until through _assemble_triage_items,
  never re-derived from a thread's raw columns (P2-1).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db
from app.main import app
from app.routes import mailbox


def _compiled(statement):
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# The shared predicate and its projection (P1-3/P2-1) -- SQL-shape only,
# since this suite runs offline against no real database.
# ---------------------------------------------------------------------------


def test_active_snooze_predicate_has_the_explicit_null_arm():
    # A bare `last_message_at <= snoozed_at` against a NULL is SQL unknown --
    # without the explicit IS NULL arm, a future-snoozed thread with no
    # last_message_at would vanish from every bucket instead of staying
    # snoozed. This pins that arm's presence so a refactor can't drop it.
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    compiled = _compiled(mailbox._active_snooze_predicate(now))
    assert "mail_thread.snoozed_until IS NOT NULL" in compiled
    assert "mail_thread.snoozed_until >" in compiled
    assert "mail_thread.last_message_at IS NULL" in compiled
    assert "mail_thread.last_message_at <= mail_thread.snoozed_at" in compiled


def test_active_snooze_predicate_binds_the_captured_now_not_db_now():
    # now is computed in Python and bound in literally -- never db now().
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    compiled = _compiled(mailbox._active_snooze_predicate(now))
    assert "now()" not in compiled.lower()
    assert "2026-08-14 12:00:00" in compiled


def test_projected_active_snoozed_until_is_a_case_over_the_shared_predicate():
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    compiled = _compiled(mailbox._projected_active_snoozed_until(now))
    assert "CASE WHEN" in compiled
    # SQLAlchemy omits an explicit `ELSE NULL` (it's the case() default), so
    # this is "yield the raw column when the predicate holds, else NULL".
    assert "THEN mail_thread.snoozed_until END" in compiled
    # Same predicate text as the standalone helper -- never re-derived.
    assert _compiled(mailbox._active_snooze_predicate(now)) in compiled


# ---------------------------------------------------------------------------
# _assemble_triage_items: exposure comes from the caller's projection, never
# from a thread's raw columns (P2-1) -- covers the "active vs stale pair"
# case, where a thread can carry non-null snoozed_until/snoozed_at (a
# leftover from before it woke -- §3.1, clearing them isn't required) while
# NOT actively snoozed.
# ---------------------------------------------------------------------------


class _AccountLookupResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _AccountLookupDB:
    def __init__(self, account_id):
        self.account_id = account_id

    def execute(self, statement):
        if "provider_account" in str(statement):
            return _AccountLookupResult(
                [(self.account_id, None, "owner@gmail.example")]
            )
        return _AccountLookupResult([])


def test_assemble_triage_items_ignores_a_stale_raw_snooze_pair():
    thread_id = uuid4()
    account_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        subject="Stale snooze",
        last_message_at=None,
        provider_account_id=account_id,
        replied_at=None,
        # Raw columns still populated from a snooze that already woke --
        # must NOT leak through when the caller's projection says "no".
        snoozed_until=datetime(2020, 1, 1, tzinfo=timezone.utc),
        snoozed_at=datetime(2019, 12, 1, tzinfo=timezone.utc),
    )
    [item] = mailbox._assemble_triage_items(
        _AccountLookupDB(account_id), [thread], {}
    )
    assert item["snoozed_until"] is None


def test_assemble_triage_items_exposes_the_projected_active_snoozed_until():
    thread_id = uuid4()
    account_id = uuid4()
    active_wake = datetime(2026, 9, 1, tzinfo=timezone.utc)
    thread = SimpleNamespace(
        id=thread_id,
        subject="Active snooze",
        last_message_at=None,
        provider_account_id=account_id,
        replied_at=None,
    )
    [item] = mailbox._assemble_triage_items(
        _AccountLookupDB(account_id), [thread], {thread_id: active_wake}
    )
    assert item["snoozed_until"] == active_wake


def test_assemble_triage_items_defaults_snoozed_until_to_none_without_a_map():
    # The optional third arg -- existing callers of this helper (unit tests
    # in test_mailbox.py) don't pass it.
    thread_id = uuid4()
    account_id = uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        subject="No projection supplied",
        last_message_at=None,
        provider_account_id=account_id,
        replied_at=None,
    )
    [item] = mailbox._assemble_triage_items(_AccountLookupDB(account_id), [thread])
    assert item["snoozed_until"] is None


# ---------------------------------------------------------------------------
# GET /mail/triage -- snoozed bucket, sort contract, and every other
# bucket's NOT active_snoozed exclusion.
# ---------------------------------------------------------------------------


@pytest.fixture
def triage_client():
    user = MagicMock(id=uuid4())
    db = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.execute.return_value.all.return_value = []
    db.execute.return_value.scalar_one.return_value = 0
    db.execute.return_value.one.return_value = (0, 0)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db
    app.dependency_overrides.clear()


def _last_statement(db):
    return db.execute.call_args[0][0]


def test_triage_snoozed_bucket_filters_open_and_actively_snoozed(triage_client):
    c, db = triage_client
    resp = c.get("/api/v1/mail/triage?bucket=snoozed")
    assert resp.status_code == 200
    compiled = _compiled(_last_statement(db))
    assert "mail_thread.done_at IS NULL" in compiled
    assert "mail_thread.snoozed_until IS NOT NULL" in compiled
    # Not negated -- the snoozed bucket wants actively-snoozed threads IN,
    # unlike every other bucket.
    assert "NOT (mail_thread.snoozed_until IS NOT NULL" not in compiled


def test_triage_snoozed_bucket_default_sort_is_wake_time_then_id_desc(triage_client):
    c, db = triage_client
    c.get("/api/v1/mail/triage?bucket=snoozed")
    compiled = _compiled(_last_statement(db))
    order_by_clause = compiled.split("ORDER BY", 1)[1]
    assert "mail_thread.snoozed_until ASC" in order_by_clause
    assert "mail_thread.id DESC" in order_by_clause
    # Not the recency ordering -- soonest wake first, not newest message.
    assert "mail_thread.last_message_at" not in order_by_clause


def test_triage_snoozed_bucket_account_sort_orders_by_account_then_wake_time(
    triage_client,
):
    c, db = triage_client
    c.get("/api/v1/mail/triage?bucket=snoozed&sort=account")
    compiled = _compiled(_last_statement(db))
    assert "JOIN provider_account" in compiled
    order_by_clause = compiled.split("ORDER BY", 1)[1]
    assert (
        "coalesce(provider_account.display_email, provider_account.external_user_id)"
        in order_by_clause
    )
    assert "mail_thread.snoozed_until ASC" in order_by_clause
    assert "mail_thread.id DESC" in order_by_clause


@pytest.mark.parametrize("bucket", ["all", "needs_reply", "unclassified"])
def test_triage_open_buckets_exclude_actively_snoozed_threads(triage_client, bucket):
    c, db = triage_client
    resp = c.get(f"/api/v1/mail/triage?bucket={bucket}")
    assert resp.status_code == 200
    compiled = _compiled(_last_statement(db))
    assert "NOT (mail_thread.snoozed_until IS NOT NULL" in compiled


def test_triage_done_bucket_does_not_filter_on_the_snooze_predicate(triage_client):
    # The projected snoozed_until still rides along in the SELECT list (every
    # triage response carries it, done bucket included -- harmless, since a
    # done thread can never have an active snooze per the exclusivity CHECK),
    # but the WHERE clause itself must not filter on it.
    c, db = triage_client
    c.get("/api/v1/mail/triage?bucket=done")
    compiled = _compiled(_last_statement(db))
    where_clause = compiled.split("WHERE", 1)[1]
    assert "snoozed_until" not in where_clause


def test_backfill_buckets_exclude_snoozed_like_done():
    assert "snoozed" not in mailbox._BACKFILL_BUCKETS
    assert "done" not in mailbox._BACKFILL_BUCKETS
    assert "snoozed" in mailbox._TRIAGE_BUCKETS


def test_backfill_snoozed_bucket_is_rejected(triage_client):
    c, _db = triage_client
    resp = c.post("/api/v1/mail/classify/backfill?bucket=snoozed")
    assert resp.status_code == 422
    assert "Invalid bucket" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /mail/counts -- snoozed key
# ---------------------------------------------------------------------------


def test_counts_response_includes_a_snoozed_key(triage_client):
    c, _db = triage_client
    resp = c.get("/api/v1/mail/counts")
    assert resp.status_code == 200
    assert "snoozed" in resp.json()["counts"]


def test_counts_issues_a_dedicated_snoozed_query_using_the_shared_predicate(
    triage_client,
):
    c, db = triage_client
    c.get("/api/v1/mail/counts")
    statements = [call.args[0] for call in db.execute.call_args_list]
    # [grouped open-non-snoozed buckets, done count, snoozed count, actions].
    assert len(statements) == 4
    snoozed_statement = statements[2]
    compiled = _compiled(snoozed_statement)
    assert "mail_thread.done_at IS NULL" in compiled
    assert "mail_thread.snoozed_until IS NOT NULL" in compiled


def test_counts_open_bucket_grouping_excludes_actively_snoozed(triage_client):
    c, db = triage_client
    c.get("/api/v1/mail/counts")
    grouped_statement = db.execute.call_args_list[0].args[0]
    assert "NOT (mail_thread.snoozed_until IS NOT NULL" in _compiled(grouped_statement)


# ---------------------------------------------------------------------------
# POST /mail/thread/{id}/snooze -- validation bounds
# ---------------------------------------------------------------------------


class _SnoozeDB:
    """Fake session backing snooze_thread and set_thread_done -- both use the
    same full-row FOR UPDATE shape (§3.3). db.get resolves `thread` (the
    pre-lock snapshot, used only for the 404 check); any db.execute() whose
    statement contains FOR UPDATE returns `locked_thread` (defaults to
    `thread` itself). Passing a DIFFERENT `locked_thread` object lets a test
    simulate a race: the pre-lock snapshot shows one state, the locked
    re-read shows whatever a concurrent request already committed.
    """

    def __init__(self, thread, *, locked_thread=None):
        self.thread = thread
        self.locked_thread = thread if locked_thread is None else locked_thread
        self.statements = []

    def get(self, model, pk):
        return self.thread if pk == self.thread.id else None

    def execute(self, statement):
        self.statements.append(statement)
        result = MagicMock()
        if "FOR UPDATE" in str(statement):
            result.scalar_one.return_value = self.locked_thread
        return result

    def commit(self):
        pass


def _snooze_client(db, user_id):
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id=user_id)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _owned_thread(thread_id, user_id, **overrides):
    defaults = dict(id=thread_id, user_id=user_id, done_at=None, snoozed_until=None, snoozed_at=None)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_snooze_naive_datetime_is_rejected():
    user_id, thread_id = uuid4(), uuid4()
    db = _SnoozeDB(_owned_thread(thread_id, user_id))
    client = _snooze_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/snooze",
            json={"until": "2026-09-01T08:00:00"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "naive_datetime"


def test_snooze_in_the_past_is_rejected():
    user_id, thread_id = uuid4(), uuid4()
    db = _SnoozeDB(_owned_thread(thread_id, user_id))
    client = _snooze_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/snooze",
            json={"until": "2020-01-01T00:00:00+00:00"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "snooze_too_soon"


def test_snooze_under_the_minimum_lead_is_rejected():
    user_id, thread_id = uuid4(), uuid4()
    db = _SnoozeDB(_owned_thread(thread_id, user_id))
    client = _snooze_client(db, user_id)
    until = datetime.now(timezone.utc) + timedelta(seconds=30)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/snooze",
            json={"until": until.isoformat()},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "snooze_too_soon"


def test_snooze_over_the_maximum_lead_is_rejected():
    user_id, thread_id = uuid4(), uuid4()
    db = _SnoozeDB(_owned_thread(thread_id, user_id))
    client = _snooze_client(db, user_id)
    until = datetime.now(timezone.utc) + timedelta(days=400)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/snooze",
            json={"until": until.isoformat()},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "snooze_too_far"


def test_snooze_404s_for_another_users_thread():
    thread = _owned_thread(uuid4(), uuid4())
    db = _SnoozeDB(thread)
    client = _snooze_client(db, uuid4())
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread.id}/snooze",
            json={"until": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /mail/thread/{id}/snooze -- snooze / unsnooze / re-snooze
# ---------------------------------------------------------------------------


def test_snooze_sets_the_pair_and_clears_done_at():
    user_id, thread_id = uuid4(), uuid4()
    thread = _owned_thread(thread_id, user_id, done_at=datetime.now(timezone.utc))
    db = _SnoozeDB(thread)
    client = _snooze_client(db, user_id)
    until = datetime.now(timezone.utc) + timedelta(days=1)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/snooze",
            json={"until": until.isoformat()},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert datetime.fromisoformat(body["snoozed_until"]) == until
    assert body["snoozed_at"] is not None
    assert thread.done_at is None


def test_unsnooze_clears_both_columns():
    user_id, thread_id = uuid4(), uuid4()
    thread = _owned_thread(
        thread_id,
        user_id,
        snoozed_until=datetime.now(timezone.utc) + timedelta(days=1),
        snoozed_at=datetime.now(timezone.utc),
    )
    db = _SnoozeDB(thread)
    client = _snooze_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/snooze", json={"until": None}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {
        "thread_id": str(thread_id),
        "snoozed_until": None,
        "snoozed_at": None,
    }
    assert thread.snoozed_until is None
    assert thread.snoozed_at is None


def test_resnooze_overwrites_the_existing_snooze():
    user_id, thread_id = uuid4(), uuid4()
    thread = _owned_thread(
        thread_id,
        user_id,
        snoozed_until=datetime.now(timezone.utc) + timedelta(days=1),
        snoozed_at=datetime.now(timezone.utc),
    )
    db = _SnoozeDB(thread)
    client = _snooze_client(db, user_id)
    new_until = datetime.now(timezone.utc) + timedelta(days=5)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/snooze",
            json={"until": new_until.isoformat()},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert datetime.fromisoformat(resp.json()["snoozed_until"]) == new_until


def test_done_clears_an_active_snooze():
    user_id, thread_id = uuid4(), uuid4()
    thread = _owned_thread(
        thread_id,
        user_id,
        snoozed_until=datetime.now(timezone.utc) + timedelta(days=1),
        snoozed_at=datetime.now(timezone.utc),
    )
    db = _SnoozeDB(thread)
    client = _snooze_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/done", json={"done": True}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["done"] is True
    assert thread.snoozed_until is None
    assert thread.snoozed_at is None


# ---------------------------------------------------------------------------
# Done/snooze exclusivity race (§3.3) -- both orderings. The pre-lock
# snapshot deliberately shows NEITHER state, so a test failing back onto the
# stale object (instead of the locked re-read) would silently pass by doing
# nothing -- these only pass if the route actually mutates `locked_thread`.
# ---------------------------------------------------------------------------


def test_snooze_then_done_race_loser_clears_the_winners_committed_snooze():
    user_id, thread_id = uuid4(), uuid4()
    stale_snapshot = _owned_thread(thread_id, user_id)
    locked_thread = _owned_thread(
        thread_id,
        user_id,
        snoozed_until=datetime.now(timezone.utc) + timedelta(days=2),
        snoozed_at=datetime.now(timezone.utc),
    )
    db = _SnoozeDB(stale_snapshot, locked_thread=locked_thread)
    client = _snooze_client(db, user_id)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/done", json={"done": True}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["done"] is True
    assert locked_thread.snoozed_until is None
    assert locked_thread.snoozed_at is None


def test_done_then_snooze_race_loser_clears_the_winners_committed_done_at():
    user_id, thread_id = uuid4(), uuid4()
    stale_snapshot = _owned_thread(thread_id, user_id)
    locked_thread = _owned_thread(
        thread_id, user_id, done_at=datetime.now(timezone.utc)
    )
    db = _SnoozeDB(stale_snapshot, locked_thread=locked_thread)
    client = _snooze_client(db, user_id)
    until = datetime.now(timezone.utc) + timedelta(days=1)
    try:
        resp = client.post(
            f"/api/v1/mail/thread/{thread_id}/snooze",
            json={"until": until.isoformat()},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert datetime.fromisoformat(body["snoozed_until"]) == until
    assert locked_thread.done_at is None
