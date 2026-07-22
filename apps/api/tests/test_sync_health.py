"""GET /mail/sync/health -- is this mailbox actually being kept current?

The endpoint exists because "last sync was 12h ago" used to be ambiguous
between "the pipeline is wedged" and "the user was asleep". It answers the data
question (`stale`) and the machinery question (`scheduler_alive`) separately, so
neither can hide the other.

Multi-account aware: the top-level fields are a worst-of aggregate across
every connected Gmail account (so the console's existing pill logic keeps
parsing this unchanged), and `accounts` breaks that aggregate down per
account.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db
from app.main import app
from app.routes import mailbox as mailbox_routes

USER_ID = uuid4()
URL = "/api/v1/mail/sync/health"


def _account(email="user@gmail.example", *, paused=False, pause_reason=None, refresh_token="rt"):
    return SimpleNamespace(
        id=uuid4(),
        external_user_id=email,
        sync_paused_at=datetime.now(timezone.utc) if paused else None,
        sync_pause_reason=pause_reason,
        refresh_token=refresh_token,
    )


class _Result:
    """Enough of a SQLAlchemy Result to support both `.all()` (the grouped
    last-success query, which wants raw rows) and `.scalars().all()` (every
    other query the endpoint runs)."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _DB:
    """Replays canned results for the endpoint's three grouped queries, in
    the order it issues them: accounts, last-success-per-account, then
    active-run account ids."""

    def __init__(self, accounts, successes, active_ids):
        self._results = [
            _Result(list(accounts)),
            _Result(list(successes.items())),
            _Result(list(active_ids)),
        ]
        self._next = 0

    def execute(self, _statement):
        result = self._results[self._next]
        self._next += 1
        return result


@pytest.fixture
def health(monkeypatch):
    """Drive the endpoint by stating the world: accounts, their last
    successes, and which have an active run."""
    monkeypatch.setattr(mailbox_routes, "expire_stale_sync", lambda db, uid: None)
    monkeypatch.setattr(
        mailbox_routes, "read_dispatcher_heartbeat", lambda: datetime.now(timezone.utc)
    )

    def _build(*, accounts=(), successes=None, active_ids=()):
        db = _DB(accounts, successes or {}, active_ids)
        user = SimpleNamespace(id=USER_ID)
        return mailbox_routes.get_sync_health(current_user=user, db=db)

    return _build


def test_requires_auth():
    assert TestClient(app).get(URL).status_code == 401


def test_literal_health_path_is_not_parsed_as_a_run_id(monkeypatch):
    # /sync/{run_id} is UUID-typed and declared right after this route, so a
    # regression in declaration order shows up as a 422 here.
    monkeypatch.setattr(mailbox_routes, "expire_stale_sync", lambda db, uid: None)
    monkeypatch.setattr(
        mailbox_routes, "read_dispatcher_heartbeat", lambda: datetime.now(timezone.utc)
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER_ID)
    app.dependency_overrides[get_db] = lambda: _DB([], {}, [])
    try:
        response = TestClient(app).get(URL)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "stale" in response.json()


def test_zero_accounts_is_reported_as_not_connected(health):
    body = health(accounts=[])
    assert body["reason"] == "not_connected"
    assert body["accounts"] == []
    assert body["stale"] is False
    assert body["sync_in_progress"] is False
    assert body["last_succeeded_at"] is None


def test_one_healthy_account(health):
    account = _account()
    now = datetime.now(timezone.utc)
    body = health(accounts=[account], successes={account.id: now})

    assert body["reason"] is None
    assert body["stale"] is False
    assert body["last_succeeded_at"] == now
    assert body["accounts"] == [
        {
            "provider_account_id": str(account.id),
            "email_address": account.external_user_id,
            "last_succeeded_at": now,
            "stale": False,
            "sync_in_progress": False,
            "reason": None,
        }
    ]


def test_two_accounts_worst_of_one_paused(health):
    healthy = _account("healthy@gmail.example")
    paused = _account("paused@gmail.example", paused=True, pause_reason="invalid_grant")
    now = datetime.now(timezone.utc)
    body = health(accounts=[healthy, paused], successes={healthy.id: now})

    # The aggregate is worst-of: one paused account drags the whole mailbox
    # to reauth_required...
    assert body["reason"] == "reauth_required"
    assert body["stale"] is False

    # ...but the per-account entries keep the honest, individual picture --
    # the healthy account doesn't get painted with the other's problem.
    entries = {entry["provider_account_id"]: entry for entry in body["accounts"]}
    assert entries[str(healthy.id)]["reason"] is None
    assert entries[str(paused.id)]["reason"] == "invalid_grant"


def test_never_synced_mix(health):
    synced = _account("synced@gmail.example")
    unsynced = _account("unsynced@gmail.example")
    now = datetime.now(timezone.utc)
    body = health(accounts=[synced, unsynced], successes={synced.id: now})

    assert body["reason"] == "never_synced"
    entries = {entry["provider_account_id"]: entry for entry in body["accounts"]}
    assert entries[str(synced.id)]["reason"] is None
    assert entries[str(unsynced.id)]["reason"] == "never_synced"
    assert entries[str(unsynced.id)]["last_succeeded_at"] is None
    # The synced account's timestamp is the only one there is -- a never-synced
    # sibling account surfaces through `reason`, not by blanking this out.
    assert body["last_succeeded_at"] == now


def test_a_missing_refresh_token_asks_for_reconnect_not_never_synced(health):
    # Google sometimes omits the refresh token on a login-path insert. That
    # account is stuck exactly like a paused one -- the dispatcher can't sync
    # without a refresh token, and only a reconnect (re-consent) mints one --
    # so it must not report "never_synced" forever, a dead end the user has
    # no way to act on.
    account = _account(refresh_token=None)
    body = health(accounts=[account])

    assert body["accounts"][0]["reason"] == "reauth_required"
    assert body["reason"] == "reauth_required"


def test_a_stale_success_is_reported_per_account_and_in_the_aggregate(health):
    account = _account()
    old = datetime.now(timezone.utc) - timedelta(hours=6)
    body = health(accounts=[account], successes={account.id: old})
    assert body["stale"] is True
    assert body["accounts"][0]["stale"] is True


def test_an_active_run_is_reported_per_account_and_in_the_aggregate(health):
    account = _account()
    now = datetime.now(timezone.utc)
    body = health(accounts=[account], successes={account.id: now}, active_ids=[account.id])
    assert body["sync_in_progress"] is True
    assert body["accounts"][0]["sync_in_progress"] is True


def test_a_revoked_token_asks_for_reconnect_instead_of_crying_stale(health):
    # Staleness the user can't act on is noise; "reconnect" is actionable.
    account = _account(paused=True, pause_reason="reauth_required")
    body = health(
        accounts=[account],
        successes={account.id: datetime.now(timezone.utc) - timedelta(days=3)},
    )
    assert body["reason"] == "reauth_required"
    assert body["stale"] is False


def test_a_dead_scheduler_shows_even_while_data_is_fresh(monkeypatch, health):
    # The whole point of splitting the two signals: the browser fallback can
    # keep mail flowing while the scheduler is dead, and we still want to know.
    monkeypatch.setattr(mailbox_routes, "read_dispatcher_heartbeat", lambda: None)
    account = _account()
    body = health(
        accounts=[account], successes={account.id: datetime.now(timezone.utc)}
    )
    assert body["stale"] is False
    assert body["scheduler_alive"] is False


def test_disabled_scheduling_is_not_reported_as_a_dead_scheduler(monkeypatch, health):
    # interval 0 is the documented off switch: no heartbeat is ever written and
    # the browser fallback carries sync, so "no heartbeat" is expected, not a
    # dead scheduler. Reporting it as down would alarm every user forever.
    monkeypatch.setattr(mailbox_routes.settings, "scheduled_sync_interval_seconds", 0)
    monkeypatch.setattr(mailbox_routes, "read_dispatcher_heartbeat", lambda: None)
    account = _account()
    body = health(
        accounts=[account], successes={account.id: datetime.now(timezone.utc)}
    )
    assert body["scheduler_alive"] is True
