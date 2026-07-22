from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from app.services.ingest import outlook_ingest


def _fake_provider(**overrides):
    defaults = dict(
        id=uuid4(),
        user_id="u1",
        access_token="tok",
        refresh_token="rt",
        token_expiry=None,
        outlook_delta_cursors=None,
        outlook_backfill_complete=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_db(provider):
    """A db whose only real job in these tests is handing back the account --
    every other query the run makes is monkeypatched at its own boundary
    (mirrors gmail_ingest's own test convention of not faking a whole ORM)."""
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = provider
    return db


def _no_removals(*_args, **_kwargs):
    return []


def _no_op_removals(*_args, **_kwargs):
    return {"verified": 0, "deleted": 0, "kept": 0}


def _upsert_stub(threads=0, messages=0, classified=0):
    return {"threads_upserted": threads, "messages_upserted": messages, "classified": classified}


# ---------------------------------------------------------------------------
# Cursor helpers: round-trip, sibling preservation, malformed recovery
# ---------------------------------------------------------------------------


def test_load_cursors_returns_empty_dict_for_none():
    account = _fake_provider(outlook_delta_cursors=None)
    assert outlook_ingest._load_cursors(account) == {}


def test_load_cursors_recovers_from_malformed_json():
    account = _fake_provider(outlook_delta_cursors="{not valid json")
    assert outlook_ingest._load_cursors(account) == {}


def test_load_cursors_recovers_from_non_object_json():
    account = _fake_provider(outlook_delta_cursors="[1, 2, 3]")
    assert outlook_ingest._load_cursors(account) == {}


def test_save_cursor_round_trips_and_never_commits():
    account = _fake_provider()
    db = MagicMock()

    outlook_ingest._save_cursor(
        db, account, "inbox",
        url="https://cursor-1", baseline_complete=False, baseline_count=5, baseline_days=90,
    )

    assert outlook_ingest._load_cursors(account) == {
        "inbox": {
            "url": "https://cursor-1",
            "baseline_complete": False,
            "baseline_count": 5,
            "baseline_days": 90,
        }
    }
    # _save_cursor only stages the attribute -- the caller's page transaction
    # owns commit, so a helper committing on its own would break page atomicity.
    db.commit.assert_not_called()


def test_save_cursor_preserves_the_sibling_folder_entry():
    account = _fake_provider()
    db = MagicMock()
    outlook_ingest._save_cursor(
        db, account, "inbox",
        url="https://inbox-cursor", baseline_complete=False, baseline_count=5, baseline_days=90,
    )

    merged = outlook_ingest._save_cursor(
        db, account, "sentitems",
        url="https://sent-cursor", baseline_complete=True, baseline_count=10, baseline_days=45,
    )

    assert merged["inbox"] == {
        "url": "https://inbox-cursor",
        "baseline_complete": False,
        "baseline_count": 5,
        "baseline_days": 90,
    }
    assert merged["sentitems"] == {
        "url": "https://sent-cursor",
        "baseline_complete": True,
        "baseline_count": 10,
        "baseline_days": 45,
    }


# ---------------------------------------------------------------------------
# Budget exhaustion / page-granular atomicity / removal-budget deferral
# ---------------------------------------------------------------------------


def test_budget_exhaustion_persists_next_url_and_ends_the_run_cleanly(monkeypatch):
    provider = _fake_provider()
    db = _make_db(provider)

    page = {
        "messages": [{"id": f"m{i}"} for i in range(10)],
        "removed_ids": [],
        "next_url": "https://next-1",
        "delta_url": None,
    }
    client = MagicMock()
    client.delta_page = MagicMock(return_value=page)
    monkeypatch.setattr(outlook_ingest, "OutlookClient", lambda token: client)
    monkeypatch.setattr(outlook_ingest, "_locally_stored_removal_rows", _no_removals)
    monkeypatch.setattr(
        outlook_ingest, "_upsert_page_messages", lambda *a, **k: _upsert_stub(messages=10)
    )
    monkeypatch.setattr(outlook_ingest, "_apply_removals", _no_op_removals)

    result = outlook_ingest.ingest_outlook_messages(
        db, "u1", provider_account_id=str(provider.id), max_results=10, max_pages=20
    )

    assert result["messages_upserted"] == 10
    # Budget exhausted after page 1 -- no second inbox page, and sentitems
    # never even gets a first request this run.
    assert client.delta_page.call_count == 1
    cursors = outlook_ingest._load_cursors(provider)
    assert cursors["inbox"]["url"] == "https://next-1"


def test_page_failure_does_not_advance_the_cursor_and_replay_refetches_only_that_page(
    monkeypatch,
):
    provider = _fake_provider()
    db = _make_db(provider)

    page1 = {
        "messages": [{"id": "m1"}], "removed_ids": [], "next_url": "https://page-2", "delta_url": None,
    }
    page2 = {
        "messages": [{"id": "m2"}], "removed_ids": [], "next_url": None, "delta_url": "https://delta-final",
    }
    client = MagicMock()
    client.delta_page = MagicMock(side_effect=[page1, page2])
    monkeypatch.setattr(outlook_ingest, "OutlookClient", lambda token: client)
    monkeypatch.setattr(outlook_ingest, "_locally_stored_removal_rows", _no_removals)
    monkeypatch.setattr(outlook_ingest, "_apply_removals", _no_op_removals)
    monkeypatch.setattr(
        outlook_ingest,
        "_upsert_page_messages",
        MagicMock(side_effect=[_upsert_stub(messages=1), RuntimeError("db exploded")]),
    )

    with pytest.raises(RuntimeError, match="db exploded"):
        outlook_ingest.ingest_outlook_messages(
            db, "u1", provider_account_id=str(provider.id), max_results=50, max_pages=20
        )

    # Page 1's additions + cursor advance landed; page 2's failure rolled back
    # before it ever reached a commit or a cursor save.
    assert db.commit.call_count == 1
    cursors = outlook_ingest._load_cursors(provider)
    assert cursors["inbox"]["url"] == "https://page-2"

    # Replay against a fresh session: only page 2 gets refetched (it's the one
    # behind the still-current cursor) -- page 1 is never re-verified.
    db2 = _make_db(provider)
    client.delta_page = MagicMock(side_effect=[page2])
    monkeypatch.setattr(
        outlook_ingest, "_upsert_page_messages", lambda *a, **k: _upsert_stub(messages=1)
    )

    # max_results=1 here (vs. 50 above) so the run stops right after this one
    # page (which reaches delta_url) instead of also walking into sentitems --
    # keeps the assertions below scoped to just the inbox replay.
    outlook_ingest.ingest_outlook_messages(
        db2, "u1", provider_account_id=str(provider.id), max_results=1, max_pages=20
    )

    assert client.delta_page.call_args.kwargs["cursor_url"] == "https://page-2"
    cursors = outlook_ingest._load_cursors(provider)
    assert cursors["inbox"]["url"] == "https://delta-final"


def test_mid_page_refresh_succeeds_but_a_later_failure_leaves_nothing_durable(monkeypatch):
    monkeypatch.setattr(outlook_ingest.settings, "microsoft_client_id", "cid")
    monkeypatch.setattr(outlook_ingest.settings, "microsoft_client_secret", "secret")

    provider = _fake_provider(refresh_token="old-rt")
    db = _make_db(provider)

    page = {
        "messages": [{"id": "m1"}], "removed_ids": ["r1"], "next_url": None, "delta_url": "https://delta-1",
    }
    client = MagicMock()
    client.delta_page = MagicMock(return_value=page)
    monkeypatch.setattr(outlook_ingest, "OutlookClient", lambda token: client)
    monkeypatch.setattr(
        outlook_ingest, "_locally_stored_removal_rows", lambda *a, **k: [("mid", "r1", "tid")]
    )
    monkeypatch.setattr(
        outlook_ingest, "_upsert_page_messages", lambda *a, **k: _upsert_stub(messages=1)
    )

    unauthorized = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=MagicMock(status_code=401)
    )
    calls = []

    def fake_apply_removals(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise unauthorized
        raise RuntimeError("verification GET failed after refresh")

    monkeypatch.setattr(outlook_ingest, "_apply_removals", fake_apply_removals)

    refresh_resp = MagicMock(status_code=200)
    refresh_resp.json.return_value = {
        "access_token": "new-at", "refresh_token": "new-rt", "expires_in": 3600,
    }
    refresh_resp.raise_for_status.side_effect = None
    monkeypatch.setattr(outlook_ingest.httpx, "post", lambda *a, **k: refresh_resp)

    token_sessions = []

    def fake_session_local():
        session = MagicMock()
        token_sessions.append(session)
        return nullcontext(session)

    monkeypatch.setattr(outlook_ingest, "SessionLocal", fake_session_local)

    with pytest.raises(RuntimeError, match="verification GET failed"):
        outlook_ingest.ingest_outlook_messages(
            db, "u1", provider_account_id=str(provider.id), max_results=50, max_pages=20
        )

    # The refresh itself durably persisted through its OWN session...
    assert len(token_sessions) == 1
    token_sessions[0].commit.assert_called_once()
    # ...but the ingest session never committed this page, and the cursor
    # never advanced: a mid-page refresh must not make partial page work
    # durable before a later failure in the same page.
    db.commit.assert_not_called()
    assert outlook_ingest._load_cursors(provider) == {}


def test_removal_budget_deferral_at_a_page_boundary_still_makes_forward_progress(monkeypatch):
    provider = _fake_provider()
    db = _make_db(provider)

    inbox_page = {
        "messages": [],
        "removed_ids": [f"r{i}" for i in range(250)],
        "next_url": None,
        "delta_url": "https://inbox-delta",
    }
    sentitems_page = {
        "messages": [{"id": "s1"}], "removed_ids": [], "next_url": None, "delta_url": "https://sent-delta",
    }

    def fake_delta_page(*, folder_key=None, cursor_url=None, received_after=None, page_size=50):
        return inbox_page if folder_key == "inbox" else sentitems_page

    client = MagicMock()
    client.delta_page = MagicMock(side_effect=fake_delta_page)
    monkeypatch.setattr(outlook_ingest, "OutlookClient", lambda token: client)

    def fake_local_rows(_db, _account_id, candidate_ids):
        # All 250 of inbox's removed ids are "locally stored moves"; sentitems
        # reports none.
        return [(f"id{i}", cid, f"t{i}") for i, cid in enumerate(candidate_ids)]

    monkeypatch.setattr(outlook_ingest, "_locally_stored_removal_rows", fake_local_rows)
    upsert_mock = MagicMock(return_value=_upsert_stub(messages=1))
    monkeypatch.setattr(outlook_ingest, "_upsert_page_messages", upsert_mock)
    apply_removals_mock = MagicMock(return_value={"verified": 0, "deleted": 0, "kept": 0})
    monkeypatch.setattr(outlook_ingest, "_apply_removals", apply_removals_mock)

    result = outlook_ingest.ingest_outlook_messages(
        db, "u1", provider_account_id=str(provider.id), max_results=50, max_pages=20
    )

    # Inbox's page had 250 locally-stored removals against a 200 budget --
    # deferred whole: no additions upserted, no removals verified, its cursor
    # never touched.
    assert upsert_mock.call_count == 1
    assert apply_removals_mock.call_count == 1
    cursors = outlook_ingest._load_cursors(provider)
    assert "inbox" not in cursors
    # ...but sentitems, in the SAME run, still made forward progress.
    assert cursors["sentitems"]["url"] == "https://sent-delta"
    assert result["messages_upserted"] == 1


# ---------------------------------------------------------------------------
# Baseline + cap-detection narrowing across runs, and the 7-day floor
# ---------------------------------------------------------------------------


def test_cap_narrowing_continues_from_persisted_baseline_days_across_runs(monkeypatch):
    provider = _fake_provider()
    outlook_ingest._save_cursor(
        MagicMock(), provider, "inbox",
        url="https://cursor-a", baseline_complete=False, baseline_count=4990, baseline_days=90,
    )

    page_a = {
        "messages": [{"id": f"m{i}"} for i in range(10)],
        "removed_ids": [], "next_url": None, "delta_url": "https://delta-a",
    }
    client = MagicMock()
    client.delta_page = MagicMock(return_value=page_a)
    monkeypatch.setattr(outlook_ingest, "OutlookClient", lambda token: client)
    monkeypatch.setattr(outlook_ingest, "_locally_stored_removal_rows", _no_removals)
    monkeypatch.setattr(
        outlook_ingest, "_upsert_page_messages", lambda *a, **k: _upsert_stub(messages=10)
    )
    monkeypatch.setattr(outlook_ingest, "_apply_removals", _no_op_removals)

    db = _make_db(provider)
    outlook_ingest.ingest_outlook_messages(
        db, "u1", provider_account_id=str(provider.id), max_results=10, max_pages=20
    )

    cursors = outlook_ingest._load_cursors(provider)
    assert cursors["inbox"] == {
        "url": None, "baseline_complete": False, "baseline_count": 0, "baseline_days": 45,
    }

    # Run 2: continuing the 45-day generation, again nearly at the cap.
    outlook_ingest._save_cursor(
        MagicMock(), provider, "inbox",
        url="https://cursor-b", baseline_complete=False, baseline_count=4995, baseline_days=45,
    )
    page_b = {
        "messages": [{"id": f"n{i}"} for i in range(10)],
        "removed_ids": [], "next_url": None, "delta_url": "https://delta-b",
    }
    client.delta_page = MagicMock(return_value=page_b)

    db2 = _make_db(provider)
    outlook_ingest.ingest_outlook_messages(
        db2, "u1", provider_account_id=str(provider.id), max_results=10, max_pages=20
    )

    cursors = outlook_ingest._load_cursors(provider)
    assert cursors["inbox"] == {
        "url": None, "baseline_complete": False, "baseline_count": 0, "baseline_days": 22,
    }


def test_cap_narrowing_accepts_and_completes_at_the_seven_day_floor(monkeypatch):
    provider = _fake_provider()
    outlook_ingest._save_cursor(
        MagicMock(), provider, "inbox",
        url="https://cursor", baseline_complete=False, baseline_count=4995, baseline_days=7,
    )
    page = {
        "messages": [{"id": f"m{i}"} for i in range(10)],
        "removed_ids": [], "next_url": None, "delta_url": "https://delta-floor",
    }
    client = MagicMock()
    client.delta_page = MagicMock(return_value=page)
    monkeypatch.setattr(outlook_ingest, "OutlookClient", lambda token: client)
    monkeypatch.setattr(outlook_ingest, "_locally_stored_removal_rows", _no_removals)
    monkeypatch.setattr(
        outlook_ingest, "_upsert_page_messages", lambda *a, **k: _upsert_stub(messages=10)
    )
    monkeypatch.setattr(outlook_ingest, "_apply_removals", _no_op_removals)

    db = _make_db(provider)
    outlook_ingest.ingest_outlook_messages(
        db, "u1", provider_account_id=str(provider.id), max_results=10, max_pages=20
    )

    cursors = outlook_ingest._load_cursors(provider)
    # At the floor the cap is accepted (documented possible gap) rather than
    # narrowed further -- this generation is done, not reset.
    assert cursors["inbox"] == {
        "url": "https://delta-floor", "baseline_complete": True, "baseline_count": 5005, "baseline_days": 7,
    }


# ---------------------------------------------------------------------------
# Expiry: new generation resets baseline_days, aggregate stays ever-completed
# ---------------------------------------------------------------------------


def test_expiry_resets_baseline_days_but_the_aggregate_stays_complete(monkeypatch):
    provider = _fake_provider(outlook_backfill_complete=True)
    outlook_ingest._save_cursor(
        MagicMock(), provider, "inbox",
        url="https://cursor-old", baseline_complete=True, baseline_count=5000, baseline_days=22,
    )
    outlook_ingest._save_cursor(
        MagicMock(), provider, "sentitems",
        url="https://sent-cursor", baseline_complete=True, baseline_count=100, baseline_days=90,
    )
    monkeypatch.setattr(outlook_ingest.settings, "outlook_backfill_days", 90)

    sentitems_page = {
        "messages": [], "removed_ids": [], "next_url": None, "delta_url": "https://sent-delta-2",
    }

    def fake_delta_page(*, folder_key=None, cursor_url=None, received_after=None, page_size=50):
        if cursor_url == "https://cursor-old":
            raise outlook_ingest.DeltaExpiredError("expired")
        return sentitems_page

    client = MagicMock()
    client.delta_page = MagicMock(side_effect=fake_delta_page)
    monkeypatch.setattr(outlook_ingest, "OutlookClient", lambda token: client)
    monkeypatch.setattr(outlook_ingest, "_locally_stored_removal_rows", _no_removals)
    monkeypatch.setattr(
        outlook_ingest, "_upsert_page_messages", lambda *a, **k: _upsert_stub()
    )
    monkeypatch.setattr(outlook_ingest, "_apply_removals", _no_op_removals)

    db = _make_db(provider)
    outlook_ingest.ingest_outlook_messages(
        db, "u1", provider_account_id=str(provider.id), max_results=50, max_pages=20
    )

    cursors = outlook_ingest._load_cursors(provider)
    assert cursors["inbox"] == {
        "url": None, "baseline_complete": False, "baseline_count": 0, "baseline_days": 90,
    }
    # Never unset once ever-completed, even though inbox just re-baselined.
    assert provider.outlook_backfill_complete is True


# ---------------------------------------------------------------------------
# Removal verification: move vs. delete, thread cleanup, dedup
# ---------------------------------------------------------------------------


def test_apply_removals_dedupes_before_checking_locally_stored_rows(monkeypatch):
    captured = {}

    def fake_local_rows(_db, _account_id, candidate_ids):
        captured["candidate_ids"] = candidate_ids
        return []

    monkeypatch.setattr(outlook_ingest, "_locally_stored_removal_rows", fake_local_rows)

    outlook_ingest._apply_removals(MagicMock(), MagicMock(), uuid4(), ["a", "b", "a", "c", "b"])

    assert captured["candidate_ids"] == ["a", "b", "c"]


def test_apply_removals_deletes_a_true_404_and_drops_its_emptied_thread():
    emptied_thread_id, surviving_thread_id = uuid4(), uuid4()
    rows = [
        (uuid4(), "msg-gone", emptied_thread_id),
        (uuid4(), "msg-moved", surviving_thread_id),
    ]
    executed = []

    def execute(stmt):
        sql = str(stmt).lower()
        executed.append(sql)
        result = MagicMock()
        if "join mail_thread" in sql:
            result.all.return_value = rows
        elif "mail_message.sent_at" in sql:
            # The 404'd message was the only one in its thread.
            result.scalars.return_value.all.return_value = []
        return result

    db = MagicMock()
    db.execute.side_effect = execute
    client = MagicMock()
    client.get_message.side_effect = lambda mid: None if mid == "msg-gone" else {"id": mid}

    result = outlook_ingest._apply_removals(
        db, client, uuid4(), ["msg-gone", "msg-moved", "msg-gone"]
    )

    assert result == {"verified": 2, "deleted": 1, "kept": 1}
    assert len([s for s in executed if "delete from mail_message" in s]) == 1
    assert len([s for s in executed if "delete from mail_thread" in s]) == 1
    # The surviving (moved) message's thread was never touched at all.
    assert [s for s in executed if "update mail_thread" in s] == []


def test_apply_removals_recomputes_recency_without_touching_subject_when_thread_survives():
    thread_id = uuid4()
    newest_survivor = datetime(2026, 1, 5, tzinfo=timezone.utc)
    rows = [(uuid4(), "msg-gone", thread_id)]
    executed = []

    def execute(stmt):
        sql = str(stmt).lower()
        executed.append(sql)
        result = MagicMock()
        if "join mail_thread" in sql:
            result.all.return_value = rows
        elif "mail_message.sent_at" in sql:
            result.scalars.return_value.all.return_value = [newest_survivor, None]
        return result

    db = MagicMock()
    db.execute.side_effect = execute
    client = MagicMock()
    client.get_message.return_value = None

    result = outlook_ingest._apply_removals(db, client, uuid4(), ["msg-gone"])

    assert result == {"verified": 1, "deleted": 1, "kept": 0}
    update_calls = [s for s in executed if "update mail_thread" in s]
    assert len(update_calls) == 1
    assert "subject" not in update_calls[0]
    assert [s for s in executed if "delete from mail_thread" in s] == []


# ---------------------------------------------------------------------------
# Token refresh: rotation persistence and permanent-failure pause
# ---------------------------------------------------------------------------


@pytest.fixture
def ms_creds(monkeypatch):
    """Microsoft OAuth credentials configured -- without this
    _refresh_and_persist_token no-ops before ever reaching the network."""
    monkeypatch.setattr(outlook_ingest.settings, "microsoft_client_id", "cid")
    monkeypatch.setattr(outlook_ingest.settings, "microsoft_client_secret", "secret")


def test_refresh_rotates_and_persists_through_an_independent_session(monkeypatch, ms_creds):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "access_token": "new-at", "refresh_token": "new-rt", "expires_in": 3600,
    }
    resp.raise_for_status.side_effect = None
    monkeypatch.setattr(outlook_ingest.httpx, "post", lambda *a, **k: resp)

    fake_session = MagicMock()
    monkeypatch.setattr(outlook_ingest, "SessionLocal", lambda: nullcontext(fake_session))

    result = outlook_ingest._refresh_and_persist_token(uuid4(), "old-rt")

    assert result[0] == "new-at"
    assert result[1] == "new-rt"
    fake_session.execute.assert_called_once()
    fake_session.commit.assert_called_once()


def test_refresh_keeps_the_old_refresh_token_when_none_is_rotated(monkeypatch, ms_creds):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"access_token": "new-at", "expires_in": 3600}
    resp.raise_for_status.side_effect = None
    monkeypatch.setattr(outlook_ingest.httpx, "post", lambda *a, **k: resp)
    monkeypatch.setattr(outlook_ingest, "SessionLocal", lambda: nullcontext(MagicMock()))

    result = outlook_ingest._refresh_and_persist_token(uuid4(), "old-rt")

    assert result == ("new-at", "old-rt", result[2])


def test_invalid_grant_pauses_the_account_and_is_terminal(monkeypatch, ms_creds):
    resp = MagicMock(status_code=400)
    resp.json.return_value = {"error": "invalid_grant"}
    monkeypatch.setattr(outlook_ingest.httpx, "post", lambda *a, **k: resp)
    paused = {}
    monkeypatch.setattr(
        outlook_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid, reason=reason)
    )

    provider_id = uuid4()
    with pytest.raises(ValueError, match="revoked"):
        outlook_ingest._refresh_and_persist_token(provider_id, "rt")

    assert paused == {"id": provider_id, "reason": "reauth_required"}


def test_aadsts_consent_revoked_description_is_also_treated_as_permanent(monkeypatch, ms_creds):
    resp = MagicMock(status_code=400)
    resp.json.return_value = {
        "error": "invalid_grant",
        "error_description": "AADSTS65001: the user or admin has not consented",
    }
    monkeypatch.setattr(outlook_ingest.httpx, "post", lambda *a, **k: resp)
    paused = {}
    monkeypatch.setattr(
        outlook_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid, reason=reason)
    )

    with pytest.raises(ValueError, match="revoked"):
        outlook_ingest._refresh_and_persist_token(uuid4(), "rt")

    assert paused["reason"] == "reauth_required"


def test_a_transient_token_failure_still_raises_for_retry(monkeypatch, ms_creds):
    resp = MagicMock(status_code=500)
    resp.json.return_value = {}
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=resp
    )
    monkeypatch.setattr(outlook_ingest.httpx, "post", lambda *a, **k: resp)
    paused = {}
    monkeypatch.setattr(
        outlook_ingest, "_pause_provider", lambda pid, reason: paused.update(id=pid)
    )

    with pytest.raises(httpx.HTTPStatusError):
        outlook_ingest._refresh_and_persist_token(uuid4(), "rt")

    assert paused == {}


def test_refresh_is_a_no_op_without_a_refresh_token_or_credentials():
    assert outlook_ingest._refresh_and_persist_token(uuid4(), None) is None
    assert outlook_ingest._refresh_and_persist_token(uuid4(), "rt") is None
