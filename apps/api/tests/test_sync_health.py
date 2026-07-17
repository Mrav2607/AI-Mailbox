"""GET /mail/sync/health -- is this mailbox actually being kept current?

The endpoint exists because "last sync was 12h ago" used to be ambiguous
between "the pipeline is wedged" and "the user was asleep". It answers the data
question (`stale`) and the machinery question (`scheduler_alive`) separately, so
neither can hide the other.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db
from app.main import app
from app.routes import mailbox as mailbox_routes


USER_ID = uuid4()
URL = "/api/v1/mail/sync/health"


def _provider(**kwargs):
    defaults = {
        "refresh_token": "rt",
        "sync_paused_at": None,
        "sync_pause_reason": None,
    }
    defaults.update(kwargs)
    return MagicMock(**defaults)


@pytest.fixture
def health(monkeypatch):
    """Drive the endpoint by stating the world: provider, last success, activity."""

    def _build(*, provider=None, last_succeeded_at=None, active=False, heartbeat=True):
        db = MagicMock()
        db.execute.return_value.scalars.return_value.first.return_value = provider
        db.scalar.return_value = last_succeeded_at
        monkeypatch.setattr(
            mailbox_routes, "active_sync", lambda _db, _uid: MagicMock() if active else None
        )
        monkeypatch.setattr(
            mailbox_routes,
            "read_dispatcher_heartbeat",
            lambda: datetime.now(timezone.utc) if heartbeat else None,
        )
        app.dependency_overrides[get_current_user] = lambda: MagicMock(id=USER_ID)
        app.dependency_overrides[get_db] = lambda: db
        return TestClient(app).get(URL).json()

    yield _build
    app.dependency_overrides.clear()


def test_requires_auth():
    assert TestClient(app).get(URL).status_code == 401


def test_literal_health_path_is_not_parsed_as_a_run_id(health):
    # /sync/{run_id} is UUID-typed and declared right after this route, so a
    # regression in declaration order shows up as a 422 here.
    body = health(provider=_provider(), last_succeeded_at=datetime.now(timezone.utc))
    assert "stale" in body


def test_a_recent_success_is_not_stale(health):
    body = health(
        provider=_provider(), last_succeeded_at=datetime.now(timezone.utc)
    )
    assert body["stale"] is False
    assert body["reason"] is None


def test_an_old_success_is_stale(health):
    body = health(
        provider=_provider(),
        last_succeeded_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    assert body["stale"] is True


def test_a_run_in_flight_suppresses_stale(health):
    # A long backfill is not a broken mailbox. Without this, a legitimate
    # 30-minute run would report as an outage.
    body = health(
        provider=_provider(),
        last_succeeded_at=datetime.now(timezone.utc) - timedelta(hours=6),
        active=True,
    )
    assert body["stale"] is False
    assert body["sync_in_progress"] is True


def test_never_synced_is_reported_as_such_not_as_stale(health):
    body = health(provider=_provider(), last_succeeded_at=None)
    assert body["reason"] == "never_synced"
    assert body["stale"] is False


def test_a_revoked_token_asks_for_reconnect_instead_of_crying_stale(health):
    # Staleness the user can't act on is noise; "reconnect" is actionable.
    body = health(
        provider=_provider(
            sync_paused_at=datetime.now(timezone.utc),
            sync_pause_reason="reauth_required",
        ),
        last_succeeded_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    assert body["reason"] == "reauth_required"
    assert body["stale"] is False


def test_no_connected_account_is_not_a_failure(health):
    body = health(provider=None, last_succeeded_at=None)
    assert body["reason"] == "not_connected"
    assert body["stale"] is False


def test_a_dead_scheduler_shows_even_while_data_is_fresh(health):
    # The whole point of splitting the two signals: the browser fallback can
    # keep mail flowing while the scheduler is dead, and we still want to know.
    body = health(
        provider=_provider(),
        last_succeeded_at=datetime.now(timezone.utc),
        heartbeat=False,
    )
    assert body["stale"] is False
    assert body["scheduler_alive"] is False


def test_a_live_scheduler_reports_alive(health):
    body = health(provider=_provider(), last_succeeded_at=datetime.now(timezone.utc))
    assert body["scheduler_alive"] is True
