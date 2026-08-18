"""Label sync tests (docs/plans/2026-08-13-label-sync-plan.md), offline
only -- MagicMock httpx/db throughout, no live database or network. Covers
Wave 1's surface: the frozen drift predicate, the §3.8 ordering-guard
protocol functions, Gmail/Outlook provider mechanics, the worker task's
orchestration, and beat registration.
"""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.services.ingest import gmail_client, outlook_client
from app.services.ingest.gmail_client import GmailClient
from app.services.ingest.outlook_client import OutlookClient
from app.services.label_sync import service
from app.services.nlp.classifier import LABELS
from app.workers import tasks_label_sync
from app.workers.celery_app import celery_app


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    return httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)


def _outlook_resp(status_code: int) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if status_code >= 400:
        resp.raise_for_status.side_effect = _http_error(status_code)
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Names, house colors
# ---------------------------------------------------------------------------


def test_display_names_cover_every_classifier_label():
    assert set(service.DISPLAY_NAMES) == set(LABELS)


def test_gmail_full_name_is_the_plain_display_name():
    assert service.gmail_label_full_name("needs_reply") == "Needs reply"


def test_outlook_category_name_is_the_plain_display_name():
    assert service.outlook_category_name("fyi") == "FYI"


def test_spam_display_name_is_junk_on_both_providers():
    # Gmail rejects a top-level user label named "Spam" (reserved system
    # label name) -- Junk keeps the two providers consistent.
    assert service.DISPLAY_NAMES["spam"] == "Junk"
    assert service.gmail_label_full_name("spam") == "Junk"
    assert service.outlook_category_name("spam") == "Junk"


def test_outlook_owned_names_cover_all_six_labels_plus_their_legacy_names():
    # Six current names + six legacy "CortexMail: <name>" names -- the strip/
    # merge filter has to catch both so a re-sync cleans out the old ones.
    assert len(service.OUTLOOK_OWNED_CATEGORY_NAMES) == 12
    assert service.outlook_category_name("spam") in service.OUTLOOK_OWNED_CATEGORY_NAMES
    assert "CortexMail: Spam" in service.OUTLOOK_OWNED_CATEGORY_NAMES


def test_gmail_colors_are_all_drawn_from_the_allowed_palette():
    for label in LABELS:
        pair = service.GMAIL_LABEL_COLORS[label]
        assert pair["backgroundColor"] in service.ALLOWED_GMAIL_LABEL_COLORS
        assert pair["textColor"] in service.ALLOWED_GMAIL_LABEL_COLORS


# ---------------------------------------------------------------------------
# The frozen drift predicate -- SQL shape + the Python-side mirror used at
# claim time, including the required never-classified / removal-completed /
# pending-target regression cases.
# ---------------------------------------------------------------------------


def test_label_drift_clause_has_all_four_frozen_terms():
    compiled = _compiled(service.label_drift_clause())
    assert "IS DISTINCT FROM mail_thread.synced_label" in compiled
    assert "IS DISTINCT FROM mail_thread.synced_message_id" in compiled
    assert "mail_thread.label_resync_needed" in compiled
    assert "mail_thread.label_sync_pending_target IS NOT NULL" in compiled


def test_label_drift_clause_guards_the_message_id_term_on_current_label():
    # The message-id OR-arm is wrapped in "current_label IS NOT NULL AND
    # ..." -- without that guard, a never-classified thread (synced pair
    # both NULL, latest_message_id NULL too) would compare NULL IS
    # DISTINCT FROM NULL = false, so this guard is what keeps it OUT of
    # that arm rather than mattering to the outcome here; what actually
    # matters is that the guard clause text is present at all so a refactor
    # can't silently drop it (P3-1).
    compiled = _compiled(service.label_drift_clause())
    assert "IS NOT NULL AND" in compiled


def test_is_drifted_never_classified_thread_is_not_drifted():
    assert not service._is_drifted(
        desired_label=None,
        latest_message_id=None,
        synced_label=None,
        synced_message_id=None,
        label_resync_needed=False,
        pending_target=None,
    )


def test_is_drifted_removal_completed_thread_is_not_drifted():
    # Desired went back to None and the applied pair was already cleared --
    # nothing left to converge (P3-1's "removal-completed" case).
    assert not service._is_drifted(
        desired_label=None,
        latest_message_id="m2",
        synced_label=None,
        synced_message_id=None,
        label_resync_needed=False,
        pending_target=None,
    )


def test_is_drifted_apply_crash_then_revert_regression():
    # A task wrote label_sync_pending_target and crashed before recording;
    # classification then reverted so desired == recorded (both None) --
    # without the pending-target OR-arm this would read "converged" and the
    # orphaned provider write would never be cleaned up (P6-1).
    assert service._is_drifted(
        desired_label=None,
        latest_message_id="m2",
        synced_label=None,
        synced_message_id=None,
        label_resync_needed=False,
        pending_target="m1",
    )


def test_is_drifted_message_id_term_only_applies_when_label_desired():
    # current_label is None -> the message-id arm must not fire even though
    # latest_message_id != synced_message_id.
    assert not service._is_drifted(
        desired_label=None,
        latest_message_id="m2",
        synced_label=None,
        synced_message_id="m1",
        label_resync_needed=False,
        pending_target=None,
    )


def test_is_drifted_message_id_term_fires_when_label_desired():
    assert service._is_drifted(
        desired_label="fyi",
        latest_message_id="m2",
        synced_label="fyi",
        synced_message_id="m1",
        label_resync_needed=False,
        pending_target=None,
    )


def test_is_drifted_resync_needed_forces_drift_even_when_pair_matches():
    assert service._is_drifted(
        desired_label="fyi",
        latest_message_id="m1",
        synced_label="fyi",
        synced_message_id="m1",
        label_resync_needed=True,
        pending_target=None,
    )


# ---------------------------------------------------------------------------
# target_for_thread -- consecutive-two-crashes pending-target regression.
# ---------------------------------------------------------------------------


def test_target_for_thread_apply_targets_the_latest_message():
    assert (
        service.target_for_thread(
            desired_label="fyi",
            latest_message_id="m2",
            synced_message_id="m1",
            inherited_pending_target=None,
        )
        == "m2"
    )


def test_target_for_thread_removal_targets_the_previous_applied_pair():
    assert (
        service.target_for_thread(
            desired_label=None,
            latest_message_id="m2",
            synced_message_id="m1",
            inherited_pending_target=None,
        )
        == "m1"
    )


def test_target_for_thread_consecutive_crashes_never_lose_the_orphan():
    # First crash: an apply left label_sync_pending_target = "m1" with
    # nothing ever recorded (synced_message_id still None). A second task
    # claims, inherits "m1", and desired has since reverted to None -- its
    # OWN target computation must still point at "m1" (not None, and not
    # some other value), so a second crash before this task even writes
    # can't lose the only trace of the possibly-applied orphan.
    target = service.target_for_thread(
        desired_label=None,
        latest_message_id="m2",
        synced_message_id=None,
        inherited_pending_target="m1",
    )
    assert target == "m1"


# ---------------------------------------------------------------------------
# Execution-time eligibility
# ---------------------------------------------------------------------------


def _account(
    *,
    provider="gmail",
    label_sync_enabled=True,
    sync_paused_at=None,
    refresh_token="rt",
    scope="https://www.googleapis.com/auth/gmail.modify",
):
    return SimpleNamespace(
        provider=provider,
        label_sync_enabled=label_sync_enabled,
        sync_paused_at=sync_paused_at,
        refresh_token=refresh_token,
        scope=scope,
    )


def test_eligibility_disabled():
    assert service.label_sync_execution_eligible(_account(label_sync_enabled=False)) == (
        False,
        "disabled",
    )


def test_eligibility_paused():
    assert service.label_sync_execution_eligible(
        _account(sync_paused_at=datetime.now(timezone.utc))
    ) == (False, "paused")


def test_eligibility_no_refresh_token():
    assert service.label_sync_execution_eligible(_account(refresh_token=None)) == (
        False,
        "no_refresh_token",
    )


def test_eligibility_gmail_missing_scope():
    assert service.label_sync_execution_eligible(
        _account(scope="https://www.googleapis.com/auth/gmail.readonly")
    ) == (False, "missing_scope")


def test_eligibility_gmail_happy_path():
    assert service.label_sync_execution_eligible(_account()) == (True, None)


def test_eligibility_outlook_missing_scope():
    assert service.label_sync_execution_eligible(
        _account(provider="outlook", scope="openid Mail.Send")
    ) == (False, "missing_scope")


def test_eligibility_outlook_happy_path():
    assert service.label_sync_execution_eligible(
        _account(provider="outlook", scope="Mail.ReadWrite")
    ) == (True, None)


# ---------------------------------------------------------------------------
# advisory lock helper
# ---------------------------------------------------------------------------


def test_advisory_lock_key_is_stable_and_fits_a_signed_bigint():
    account_id = uuid4()
    key = service.advisory_lock_key(account_id)
    assert key == service.advisory_lock_key(account_id)
    assert 0 <= key <= 0x7FFFFFFFFFFFFFFF


def test_acquire_account_lock_issues_pg_advisory_xact_lock():
    db = MagicMock()
    account_id = uuid4()
    service.acquire_account_lock(db, account_id)
    args, kwargs = db.execute.call_args
    assert "pg_advisory_xact_lock" in str(args[0])
    assert args[1] == {"key": service.advisory_lock_key(account_id)}


# ---------------------------------------------------------------------------
# claim_thread (§3.8 step 1) -- claim/lease, expiry reclaim, eligibility
# short-circuit. db.execute is stubbed by call ORDER (this module's own
# implementation controls that order), mirroring test_reply_reconcile.py's
# _FakeDB decomposition.
# ---------------------------------------------------------------------------


def _exec_result(*, first=None, scalar_one_or_none=None):
    result = MagicMock()
    result.first.return_value = first
    result.scalar_one_or_none.return_value = scalar_one_or_none
    return result


def _thread_row(
    *,
    id=None,
    provider_thread_id="t1",
    synced_label=None,
    synced_message_id=None,
    label_resync_needed=False,
    label_sync_claim_token=None,
    label_sync_claimed_at=None,
    label_sync_pending_target=None,
):
    return SimpleNamespace(
        id=id or uuid4(),
        provider_thread_id=provider_thread_id,
        synced_label=synced_label,
        synced_message_id=synced_message_id,
        label_resync_needed=label_resync_needed,
        label_sync_claim_token=label_sync_claim_token,
        label_sync_claimed_at=label_sync_claimed_at,
        label_sync_pending_target=label_sync_pending_target,
    )


def _claim_db(*, account, locked_thread, desired_row):
    db = MagicMock()
    account_id = account.id if account is not None else uuid4()
    db.execute.side_effect = [
        _exec_result(first=SimpleNamespace(provider_account_id=account_id)),
        MagicMock(),  # advisory lock
        _exec_result(scalar_one_or_none=locked_thread),
        _exec_result(first=desired_row),
    ]
    db.get.return_value = account
    return db


def _account_row(**overrides):
    defaults = dict(
        id=uuid4(),
        provider="gmail",
        access_token="at",
        refresh_token="rt",
        scope="https://www.googleapis.com/auth/gmail.modify",
        sync_paused_at=None,
        label_sync_enabled=True,
        gmail_label_map=None,
        label_sync_generation=0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_claim_thread_returns_none_when_account_not_eligible():
    account = _account_row(label_sync_enabled=False)
    thread_id = uuid4()
    db = _claim_db(account=account, locked_thread=None, desired_row=None)
    assert service.claim_thread(db, thread_id) is None
    db.commit.assert_not_called()


def test_claim_thread_returns_none_when_already_converged():
    account = _account_row()
    thread_id = uuid4()
    locked = _thread_row(synced_label="fyi", synced_message_id="m1")
    desired_row = SimpleNamespace(label="fyi", provider_message_id="m1")
    db = _claim_db(account=account, locked_thread=locked, desired_row=desired_row)
    assert service.claim_thread(db, thread_id) is None
    db.commit.assert_not_called()


def test_claim_thread_returns_none_when_lease_is_live():
    account = _account_row()
    thread_id = uuid4()
    locked = _thread_row(
        synced_label=None,
        synced_message_id=None,
        label_sync_claim_token="other-task",
        label_sync_claimed_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    desired_row = SimpleNamespace(label="fyi", provider_message_id="m1")
    db = _claim_db(account=account, locked_thread=locked, desired_row=desired_row)
    assert service.claim_thread(db, thread_id) is None
    db.commit.assert_not_called()


def test_claim_thread_reclaims_an_expired_lease_and_preserves_pending_target():
    account = _account_row()
    thread_id = uuid4()
    locked = _thread_row(
        synced_label=None,
        synced_message_id=None,
        label_sync_claim_token="dead-task",
        label_sync_claimed_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        label_sync_pending_target="orphan-target",
    )
    desired_row = SimpleNamespace(label="fyi", provider_message_id="m1")
    db = _claim_db(account=account, locked_thread=locked, desired_row=desired_row)

    result = service.claim_thread(db, thread_id)

    assert result is not None
    assert result.desired_label == "fyi"
    assert result.latest_message_id == "m1"
    assert result.inherited_pending_target == "orphan-target"
    assert result.generation == 0
    # A fresh token was written, and the STOLEN lease's pending target was
    # left untouched (§3.8 step 1's explicit instruction).
    assert locked.label_sync_claim_token == result.claim_token
    assert locked.label_sync_claim_token != "dead-task"
    assert locked.label_sync_pending_target == "orphan-target"
    db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# set_pending_target (§3.8 step 1b)
# ---------------------------------------------------------------------------


def test_set_pending_target_success():
    db = MagicMock()
    db.execute.return_value = MagicMock(rowcount=1)
    assert service.set_pending_target(db, uuid4(), "tok", "m2") is True
    db.commit.assert_called_once()


def test_set_pending_target_lease_stolen():
    db = MagicMock()
    db.execute.return_value = MagicMock(rowcount=0)
    assert service.set_pending_target(db, uuid4(), "tok", "m2") is False


# ---------------------------------------------------------------------------
# record_sync_result (§3.8 step 6) -- lease loss and generation loss, tested
# independently (both required regressions).
# ---------------------------------------------------------------------------


def test_record_sync_result_lease_lost():
    db = MagicMock()
    db.execute.side_effect = [_exec_result(scalar_one_or_none=None)]
    outcome = service.record_sync_result(
        db,
        uuid4(),
        "tok",
        account_id=uuid4(),
        generation=1,
        applied_label="fyi",
        applied_message_id="m1",
    )
    assert outcome == "lease_lost"
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_record_sync_result_generation_lost_clears_only_claim_fields():
    locked = _thread_row(
        synced_label=None,
        synced_message_id=None,
        label_resync_needed=True,
        label_sync_claim_token="tok",
        label_sync_claimed_at=datetime.now(timezone.utc),
        label_sync_pending_target="m1",
    )
    db = MagicMock()
    db.execute.side_effect = [
        _exec_result(scalar_one_or_none=locked),
        _exec_result(scalar_one_or_none=2),  # generation moved from 1 to 2
    ]
    outcome = service.record_sync_result(
        db,
        uuid4(),
        "tok",
        account_id=uuid4(),
        generation=1,
        applied_label="fyi",
        applied_message_id="m1",
    )
    assert outcome == "generation_lost"
    assert locked.label_sync_claim_token is None
    assert locked.label_sync_claimed_at is None
    # NOT cleared -- the newer generation's tick must reconverge from
    # scratch and the pending target must stay discoverable.
    assert locked.label_resync_needed is True
    assert locked.label_sync_pending_target == "m1"
    assert locked.synced_label is None
    db.commit.assert_called_once()


def test_record_sync_result_recorded_clears_everything():
    locked = _thread_row(
        synced_label=None,
        synced_message_id=None,
        label_resync_needed=True,
        label_sync_claim_token="tok",
        label_sync_claimed_at=datetime.now(timezone.utc),
        label_sync_pending_target="m1",
    )
    db = MagicMock()
    db.execute.side_effect = [
        _exec_result(scalar_one_or_none=locked),
        _exec_result(scalar_one_or_none=1),
    ]
    outcome = service.record_sync_result(
        db,
        uuid4(),
        "tok",
        account_id=uuid4(),
        generation=1,
        applied_label="fyi",
        applied_message_id="m1",
    )
    assert outcome == "recorded"
    assert locked.synced_label == "fyi"
    assert locked.synced_message_id == "m1"
    assert locked.label_resync_needed is False
    assert locked.label_sync_pending_target is None
    assert locked.label_sync_claim_token is None
    db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# persist_gmail_label_map (§3.8 step 5) -- generation fence + malformed JSON
# tolerance + read-merge-write.
# ---------------------------------------------------------------------------


def test_load_gmail_label_map_tolerates_malformed_json():
    assert service.load_gmail_label_map(None) == {}
    assert service.load_gmail_label_map("not json") == {}
    assert service.load_gmail_label_map("[1, 2]") == {}
    assert service.load_gmail_label_map('{"a": "b"}') == {"a": "b"}


def test_persist_gmail_label_map_merges_without_clobbering_existing_entries():
    account = _account_row(gmail_label_map='{"FYI": "L1"}', label_sync_generation=3)
    db = MagicMock()
    db.execute.side_effect = [_exec_result(scalar_one_or_none=account)]
    service.persist_gmail_label_map(
        db, account.id, generation=3, learned={"Needs reply": "L2"}
    )
    import json

    saved = json.loads(account.gmail_label_map)
    assert saved == {"FYI": "L1", "Needs reply": "L2"}
    db.commit.assert_called_once()


def test_persist_gmail_label_map_drops_writes_from_a_stale_generation():
    account = _account_row(gmail_label_map='{"FYI": "L1"}', label_sync_generation=5)
    db = MagicMock()
    db.execute.side_effect = [_exec_result(scalar_one_or_none=account)]
    service.persist_gmail_label_map(
        db, account.id, generation=3, learned={"Needs reply": "L2"}
    )
    # Untouched -- generation moved past what this task captured at claim.
    assert account.gmail_label_map == '{"FYI": "L1"}'
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Gmail provider mechanics
# ---------------------------------------------------------------------------


def _needed_gmail_names() -> list[str]:
    return [service.gmail_label_full_name(lbl) for lbl in LABELS]


def test_apply_gmail_labels_creates_all_six_labels_with_house_colors():
    client = MagicMock()
    client.list_labels.return_value = {"labels": []}  # a brand-new mailbox, nothing to adopt
    ids = iter(f"L{i}" for i in range(6))
    client.create_label.side_effect = lambda payload: {"id": next(ids)}
    client.get_thread_minimal.return_value = {"messages": [{"labelIds": []}]}

    result = service.apply_gmail_labels(
        client, provider_thread_id="t1", cached_map={}, desired_label="needs_reply"
    )

    created_names = [call.args[0]["name"] for call in client.create_label.call_args_list]
    assert set(created_names) == set(_needed_gmail_names())
    # Every one of the six gets a color from the allowed palette -- there's
    # no parent label anymore to carry the old no-color special case.
    for call in client.create_label.call_args_list:
        assert call.args[0]["color"] in service.GMAIL_LABEL_COLORS.values()

    desired_id = result.working_map[service.gmail_label_full_name("needs_reply")]
    client.modify_thread.assert_called_once_with("t1", add_ids=[desired_id], remove_ids=[])


def test_apply_gmail_labels_unions_labelids_across_messages():
    names = _needed_gmail_names()
    cached_map = {name: f"ID-{name}" for name in names}
    client = MagicMock()
    needs_reply_id = cached_map[service.gmail_label_full_name("needs_reply")]
    action_required_id = cached_map[service.gmail_label_full_name("action_required")]
    # Labels differ across the thread's messages (required test).
    client.get_thread_minimal.return_value = {
        "messages": [
            {"labelIds": [needs_reply_id]},
            {"labelIds": [action_required_id]},
        ]
    }

    result = service.apply_gmail_labels(
        client,
        provider_thread_id="t1",
        cached_map=cached_map,
        desired_label="needs_reply",
    )

    # needs_reply already present -> nothing to add; action_required is a
    # present-but-undesired owned id -> removed. spam/etc never touched
    # since they were never present.
    assert result.add_ids == []
    assert result.remove_ids == [action_required_id]
    client.create_label.assert_not_called()


def test_apply_gmail_labels_removal_strips_all_present_owned_ids():
    names = _needed_gmail_names()
    cached_map = {name: f"ID-{name}" for name in names}
    fyi_id = cached_map[service.gmail_label_full_name("fyi")]
    client = MagicMock()
    client.get_thread_minimal.return_value = {"messages": [{"labelIds": [fyi_id]}]}

    result = service.apply_gmail_labels(
        client, provider_thread_id="t1", cached_map=cached_map, desired_label=None
    )

    assert result.add_ids == []
    assert result.remove_ids == [fyi_id]


def test_apply_gmail_labels_rebuilds_once_on_create_conflict_then_raises_if_still_missing():
    client = MagicMock()
    client.create_label.side_effect = _http_error(409)
    # The listing is missing "Junk" (spam's new name) -- adoption can't find
    # it, so the original conflict error propagates instead of looping
    # forever. The empty cached map means the pre-listing migration pass
    # runs first (it doesn't spend the rebuild budget), and then the
    # create-conflict path spends its own once-per-run rebuild -- both
    # listing calls see the same missing name, so two calls total.
    missing = service.gmail_label_full_name("spam")
    client.list_labels.return_value = {
        "labels": [
            {"name": name, "id": f"ADOPTED-{name}"}
            for name in _needed_gmail_names()
            if name != missing
        ]
    }

    with pytest.raises(httpx.HTTPStatusError):
        service.apply_gmail_labels(
            client, provider_thread_id="t1", cached_map={}, desired_label="spam"
        )

    assert client.list_labels.call_count == 2


def test_apply_gmail_labels_401_propagates_without_spending_the_rebuild():
    # A 401 is the outer with_token_retry's cue to refresh -- it must not be
    # mistaken for a label conflict/invalid-id and consume the once-per-run
    # rebuild budget.
    stale_map = {name: f"STALE-{name}" for name in _needed_gmail_names()}
    client = MagicMock()
    client.get_thread_minimal.side_effect = _http_error(401)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        service.apply_gmail_labels(
            client, provider_thread_id="t1", cached_map=stale_map, desired_label="fyi"
        )

    assert excinfo.value.response.status_code == 401
    client.list_labels.assert_not_called()


def test_apply_gmail_labels_rebuilds_once_on_invalid_cached_id_at_modify_time():
    names = _needed_gmail_names()
    stale_map = {name: f"STALE-{name}" for name in names}
    client = MagicMock()
    client.get_thread_minimal.side_effect = [_http_error(400), {"messages": [{"labelIds": []}]}]
    client.list_labels.return_value = {
        "labels": [{"name": name, "id": f"FRESH-{name}"} for name in names]
    }

    result = service.apply_gmail_labels(
        client, provider_thread_id="t1", cached_map=stale_map, desired_label="fyi"
    )

    assert client.list_labels.call_count == 1
    fresh_id = f"FRESH-{service.gmail_label_full_name('fyi')}"
    assert result.working_map[service.gmail_label_full_name("fyi")] == fresh_id
    client.modify_thread.assert_called_once_with("t1", add_ids=[fresh_id], remove_ids=[])


def test_apply_gmail_labels_recreates_a_deleted_label_after_a_stale_id_fails_modify():
    # L-1: the cached id for "fyi" points at a label that's since been
    # DELETED -- the re-list omits it entirely, so adopt-by-name alone can't
    # repair the mapping. The stale id must be dropped (not just left
    # unmatched) and the label recreated, not left broken forever.
    names = _needed_gmail_names()
    stale_map = {name: f"STALE-{name}" for name in names}
    fyi_name = service.gmail_label_full_name("fyi")
    client = MagicMock()
    client.get_thread_minimal.side_effect = [_http_error(400), {"messages": [{"labelIds": []}]}]
    client.list_labels.return_value = {
        "labels": [
            {"name": name, "id": f"FRESH-{name}"} for name in names if name != fyi_name
        ]
    }
    client.create_label.return_value = {"id": "NEW-fyi"}

    result = service.apply_gmail_labels(
        client, provider_thread_id="t1", cached_map=stale_map, desired_label="fyi"
    )

    # The L-1 retry path's own rebuild leaves "fyi" still missing (it's
    # genuinely gone from the listing), so `_ensure_gmail_label_ids`'s own
    # missing-name check fires a second listing call before falling back to
    # create -- both calls see the same fresh listing.
    assert client.list_labels.call_count == 2
    # Only the deleted label was recreated -- everything else came back off
    # the fresh listing.
    client.create_label.assert_called_once()
    assert client.create_label.call_args.args[0]["name"] == fyi_name
    assert result.working_map[fyi_name] == "NEW-fyi"
    for name in names:
        if name != fyi_name:
            assert result.working_map[name] == f"FRESH-{name}"
    client.modify_thread.assert_called_once_with("t1", add_ids=["NEW-fyi"], remove_ids=[])


# ---------------------------------------------------------------------------
# One-shot migration off the pre-rename Gmail labels/parent.
# ---------------------------------------------------------------------------


def test_apply_gmail_labels_migrates_legacy_labels_by_renaming_in_place():
    # An empty cached map + a listing full of the OLD names: every child
    # gets renamed (not recreated) so it keeps its id, color, and existing
    # thread applications, and the dead parent gets cleaned up.
    client = MagicMock()
    legacy_ids = {label: f"LID-{label}" for label in LABELS}
    client.list_labels.return_value = {
        "labels": [
            {"name": service.LEGACY_GMAIL_LABEL_NAMES[label], "id": legacy_ids[label]}
            for label in LABELS
        ]
        + [{"name": service.LEGACY_GMAIL_PARENT_LABEL, "id": "PARENT-ID"}]
    }
    client.get_thread_minimal.return_value = {"messages": [{"labelIds": []}]}
    client.update_label.side_effect = lambda label_id, payload: {"id": label_id, **payload}

    result = service.apply_gmail_labels(
        client, provider_thread_id="t1", cached_map={}, desired_label=None
    )

    assert client.update_label.call_count == 6
    client.create_label.assert_not_called()
    for label in LABELS:
        new_name = service.gmail_label_full_name(label)
        client.update_label.assert_any_call(legacy_ids[label], {"name": new_name})
        # Same id as the legacy label -- renaming keeps color and every
        # existing thread application, unlike a create-from-scratch.
        assert result.working_map[new_name] == legacy_ids[label]
    client.delete_label.assert_called_once_with("PARENT-ID")


def test_apply_gmail_labels_pre_listing_adopts_when_new_names_already_exist():
    # Empty cached map, but the listing already has all six current names
    # (e.g. a mailbox migrated by a previous run) -- pure adoption, no
    # renames or creates needed.
    client = MagicMock()
    new_ids = {label: f"NID-{label}" for label in LABELS}
    client.list_labels.return_value = {
        "labels": [
            {"name": service.gmail_label_full_name(label), "id": new_ids[label]}
            for label in LABELS
        ]
    }
    client.get_thread_minimal.return_value = {"messages": [{"labelIds": []}]}

    result = service.apply_gmail_labels(
        client, provider_thread_id="t1", cached_map={}, desired_label=None
    )

    client.create_label.assert_not_called()
    client.update_label.assert_not_called()
    for label in LABELS:
        assert result.working_map[service.gmail_label_full_name(label)] == new_ids[label]
    assert client.list_labels.call_count == 1


def test_apply_gmail_labels_removal_intersection_includes_a_lingering_legacy_id():
    # A thread still carries a legacy label id from before the migration --
    # the removal intersection has to catch it even though it's not one of
    # the six current names.
    cached_map = {name: f"ID-{name}" for name in _needed_gmail_names()}
    legacy_fyi_name = service.LEGACY_GMAIL_LABEL_NAMES["fyi"]
    legacy_fyi_id = "LEGACY-FYI-ID"
    cached_map[legacy_fyi_name] = legacy_fyi_id
    client = MagicMock()
    client.get_thread_minimal.return_value = {"messages": [{"labelIds": [legacy_fyi_id]}]}

    result = service.apply_gmail_labels(
        client, provider_thread_id="t1", cached_map=cached_map, desired_label=None
    )

    assert result.remove_ids == [legacy_fyi_id]
    client.modify_thread.assert_called_once_with("t1", add_ids=[], remove_ids=[legacy_fyi_id])


def test_apply_gmail_labels_adopts_new_name_over_renaming_when_both_exist():
    # The user's mailbox happens to already have a top-level "Needs reply"
    # label of their own AND the legacy "CortexMail/Needs reply" -- the new
    # name's exact-match adoption wins, the legacy one is left alone (never
    # renamed into), and its id still gets swept off the thread.
    client = MagicMock()
    client.list_labels.return_value = {
        "labels": [
            {"name": "Needs reply", "id": "USER-OWNED-ID"},
            {"name": "CortexMail/Needs reply", "id": "LEGACY-ID"},
        ]
    }
    client.create_label.side_effect = lambda payload: {"id": f"NEW-{payload['name']}"}
    client.get_thread_minimal.return_value = {"messages": [{"labelIds": ["LEGACY-ID"]}]}

    result = service.apply_gmail_labels(
        client, provider_thread_id="t1", cached_map={}, desired_label="needs_reply"
    )

    assert result.working_map["Needs reply"] == "USER-OWNED-ID"
    client.update_label.assert_not_called()
    assert "LEGACY-ID" in result.remove_ids


def test_apply_gmail_labels_parent_delete_failure_does_not_fail_the_sync():
    client = MagicMock()
    client.list_labels.return_value = {
        "labels": [
            {"name": service.gmail_label_full_name(label), "id": f"ID-{label}"}
            for label in LABELS
        ]
        + [{"name": service.LEGACY_GMAIL_PARENT_LABEL, "id": "PARENT-ID"}]
    }
    client.delete_label.side_effect = _http_error(500)
    client.get_thread_minimal.return_value = {"messages": [{"labelIds": []}]}

    result = service.apply_gmail_labels(
        client, provider_thread_id="t1", cached_map={}, desired_label=None
    )

    client.delete_label.assert_called_once_with("PARENT-ID")
    # Cosmetic cleanup only -- the dead entry is dropped from the map either
    # way, and nothing above propagated.
    assert service.LEGACY_GMAIL_PARENT_LABEL not in result.working_map


def test_apply_gmail_labels_parent_delete_401_propagates():
    client = MagicMock()
    client.list_labels.return_value = {
        "labels": [
            {"name": service.gmail_label_full_name(label), "id": f"ID-{label}"}
            for label in LABELS
        ]
        + [{"name": service.LEGACY_GMAIL_PARENT_LABEL, "id": "PARENT-ID"}]
    }
    client.delete_label.side_effect = _http_error(401)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        service.apply_gmail_labels(
            client, provider_thread_id="t1", cached_map={}, desired_label=None
        )

    assert excinfo.value.response.status_code == 401


# ---------------------------------------------------------------------------
# Outlook provider mechanics
# ---------------------------------------------------------------------------


def test_strip_owned_categories_404_on_target_is_satisfied():
    client = MagicMock()
    client.get_message_categories.return_value = None
    assert service._strip_owned_categories(client, "m1") == "absent"
    client.set_message_categories.assert_not_called()


def test_strip_owned_categories_exact_name_match_only():
    client = MagicMock()
    client.get_message_categories.return_value = {
        "categories": ["FYI", "FYI Weekly", "Personal"],
        "etag": "W/1",
    }
    client.set_message_categories.return_value = _outlook_resp(200)

    outcome = service._strip_owned_categories(client, "m1")

    assert outcome == "cleaned"
    kept = client.set_message_categories.call_args.args[1]
    # Only the exact owned name is removed -- "FYI Weekly" is a near-miss
    # that must survive untouched.
    assert kept == ["FYI Weekly", "Personal"]


def test_strip_owned_categories_412_remerges_once():
    client = MagicMock()
    client.get_message_categories.side_effect = [
        {"categories": ["FYI"], "etag": "W/1"},
        {"categories": ["FYI", "New"], "etag": "W/2"},
    ]
    client.set_message_categories.side_effect = [_outlook_resp(412), _outlook_resp(200)]

    outcome = service._strip_owned_categories(client, "m1")

    assert outcome == "cleaned"
    assert client.get_message_categories.call_count == 2
    assert client.set_message_categories.call_count == 2
    final_kept = client.set_message_categories.call_args.args[1]
    assert final_kept == ["New"]


def test_merge_desired_category_404_on_current_target_raises_item_failure():
    client = MagicMock()
    client.get_message_categories.return_value = None
    with pytest.raises(service.OutlookItemNotFound):
        service._merge_desired_category(client, "m1", "FYI")


def test_merge_desired_category_happy_path_appends_desired():
    client = MagicMock()
    client.get_message_categories.return_value = {"categories": ["Personal"], "etag": "W/1"}
    client.set_message_categories.return_value = _outlook_resp(200)
    service._merge_desired_category(client, "m1", "FYI")
    merged = client.set_message_categories.call_args.args[1]
    assert merged == ["Personal", "FYI"]


def test_apply_outlook_category_cleans_previous_target_before_new_merge():
    calls: list[tuple] = []
    client = MagicMock()

    def fake_strip(c, message_id):
        calls.append(("strip", message_id))
        return "cleaned"

    def fake_merge(c, message_id, desired_name):
        calls.append(("merge", message_id, desired_name))

    import app.services.label_sync.service as svc

    orig_strip, orig_merge = svc._strip_owned_categories, svc._merge_desired_category
    svc._strip_owned_categories = fake_strip
    svc._merge_desired_category = fake_merge
    try:
        svc.apply_outlook_category(
            client, new_target="m2", previous_target="m1", desired_name="FYI"
        )
    finally:
        svc._strip_owned_categories = orig_strip
        svc._merge_desired_category = orig_merge

    assert calls == [("strip", "m1"), ("merge", "m2", "FYI")]


def test_apply_outlook_category_skips_cleanup_when_target_unchanged():
    client = MagicMock()
    client.get_message_categories.return_value = {"categories": [], "etag": "W/1"}
    client.set_message_categories.return_value = _outlook_resp(200)
    service.apply_outlook_category(
        client, new_target="m1", previous_target="m1", desired_name="FYI"
    )
    # Only the merge's own GET happens -- no separate cleanup round trip.
    assert client.get_message_categories.call_count == 1


def test_merge_desired_category_strips_legacy_name_and_keeps_user_category():
    # A message still carries the old "CortexMail: FYI" category plus the
    # user's own "Client A" -- the merge has to strip the legacy name
    # (exact match, part of OUTLOOK_OWNED_CATEGORY_NAMES), leave the user's
    # category alone, and merge on the new plain name.
    client = MagicMock()
    client.get_message_categories.return_value = {
        "categories": ["CortexMail: FYI", "Client A"],
        "etag": "W/1",
    }
    client.set_message_categories.return_value = _outlook_resp(200)

    service._merge_desired_category(client, "m1", "FYI")

    merged = client.set_message_categories.call_args.args[1]
    assert merged == ["Client A", "FYI"]


def test_merge_desired_category_legacy_spam_category_merges_as_junk():
    client = MagicMock()
    client.get_message_categories.return_value = {
        "categories": ["CortexMail: Spam"],
        "etag": "W/1",
    }
    client.set_message_categories.return_value = _outlook_resp(200)

    service._merge_desired_category(client, "m1", service.outlook_category_name("spam"))

    merged = client.set_message_categories.call_args.args[1]
    assert merged == ["Junk"]


# ---------------------------------------------------------------------------
# Label-path client retry sleeps are capped at 30s (plan §3.1 P4-2) -- a
# task's claim lease is 10 minutes, so honoring a large Retry-After verbatim
# (as the shared ingest `_get` deliberately does) could let a task sleep
# past a STOLEN lease and keep writing after another task has taken over.
# These exercise the real OutlookClient/GmailClient retry loops (not the
# service-layer mocks above), same fake-pooled-client idiom as
# test_outlook_client.py/test_gmail_client.py.
# ---------------------------------------------------------------------------


def _http_resp(status_code=200, payload=None, headers=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = payload if payload is not None else {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.side_effect = None
    return resp


def _fake_pooled_client(**method_side_effects):
    fake = MagicMock()
    for method, side_effect in method_side_effects.items():
        setattr(fake, method, MagicMock(side_effect=side_effect))
    return fake


def test_outlook_get_message_categories_missing_etag_raises_missing_etag_error(monkeypatch):
    # L3: Graph may omit @odata.etag from a $select response -- must raise a
    # clear, dedicated exception instead of letting a None etag reach the
    # If-Match header downstream.
    ok = _http_resp(200, {"categories": ["FYI"]})  # no @odata.etag
    fake = _fake_pooled_client(get=[ok])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    with pytest.raises(outlook_client.MissingEtagError) as excinfo:
        OutlookClient("tok").get_message_categories("m1")

    assert excinfo.value.message_id == "m1"


def test_outlook_get_message_categories_huge_retry_after_fails_without_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(outlook_client.time, "sleep", lambda s: slept.append(s))
    throttled = _http_resp(429, headers={"Retry-After": "900"})
    fake = _fake_pooled_client(get=[throttled])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    with pytest.raises(httpx.HTTPStatusError):
        OutlookClient("tok").get_message_categories("m1")

    assert slept == []  # never slept at all -- failed the attempt immediately
    assert fake.get.call_count == 1  # no retry attempt either


def test_outlook_set_message_categories_huge_retry_after_fails_without_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(outlook_client.time, "sleep", lambda s: slept.append(s))
    throttled = _http_resp(429, headers={"Retry-After": "900"})
    fake = _fake_pooled_client(patch=[throttled])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    resp = OutlookClient("tok").set_message_categories("m1", ["FYI"], etag="W/1")

    assert slept == []
    assert resp.status_code == 429
    assert fake.patch.call_count == 1


def test_outlook_get_message_categories_retry_after_under_cap_still_sleeps_and_retries(monkeypatch):
    slept = []
    monkeypatch.setattr(outlook_client.time, "sleep", lambda s: slept.append(s))
    throttled = _http_resp(429, headers={"Retry-After": "5"})
    ok = _http_resp(200, {"categories": ["FYI"], "@odata.etag": "W/2"})
    fake = _fake_pooled_client(get=[throttled, ok])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    result = OutlookClient("tok").get_message_categories("m1")

    assert slept == [5.0]
    assert result == {"categories": ["FYI"], "etag": "W/2"}


def test_gmail_modify_thread_5xx_backoff_is_capped(monkeypatch):
    slept = []
    monkeypatch.setattr(gmail_client.time, "sleep", lambda s: slept.append(s))
    failing = [_http_resp(503), _http_resp(503), _http_resp(503)]
    fake = _fake_pooled_client(post=failing)
    monkeypatch.setattr(gmail_client, "_client", lambda: fake)

    with pytest.raises(httpx.HTTPStatusError):
        GmailClient("tok").modify_thread("t1", add_ids=["L1"], remove_ids=[])

    assert all(s <= gmail_client._LABEL_SYNC_MAX_RETRY_SLEEP for s in slept)


# ---------------------------------------------------------------------------
# sync_thread_labels orchestration -- lease loss, budget cap, invalid_grant.
# ---------------------------------------------------------------------------


def _claim_result(**overrides) -> service.ClaimResult:
    defaults = dict(
        thread_id=uuid4(),
        provider_thread_id="t1",
        claim_token="tok",
        generation=0,
        account=service.AccountSnapshot(
            id=uuid4(),
            provider="gmail",
            access_token="at",
            refresh_token="rt",
            scope="https://www.googleapis.com/auth/gmail.modify",
            gmail_label_map=None,
        ),
        desired_label="fyi",
        latest_message_id="m2",
        synced_label=None,
        synced_message_id=None,
        inherited_pending_target=None,
    )
    defaults.update(overrides)
    return service.ClaimResult(**defaults)


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch):
    monkeypatch.setattr(tasks_label_sync, "SessionLocal", lambda: nullcontext(MagicMock()))


def _patch_passthrough_token_retry(monkeypatch):
    monkeypatch.setattr(
        tasks_label_sync,
        "_token_retry",
        lambda account: (lambda: MagicMock(), lambda call: call()),
    )


def test_sync_thread_labels_skips_when_not_claimed(monkeypatch):
    monkeypatch.setattr(tasks_label_sync, "claim_thread", lambda db, tid: None)
    result = tasks_label_sync.sync_thread_labels.run(str(uuid4()))
    assert result["status"] == "skipped"


def test_sync_thread_labels_lease_lost_at_pending_target(monkeypatch):
    claim = _claim_result()
    monkeypatch.setattr(tasks_label_sync, "claim_thread", lambda db, tid: claim)
    monkeypatch.setattr(tasks_label_sync, "set_pending_target", lambda *a, **k: False)
    _patch_passthrough_token_retry(monkeypatch)
    apply_mock = MagicMock()
    monkeypatch.setattr(tasks_label_sync, "apply_gmail_labels", apply_mock)

    result = tasks_label_sync.sync_thread_labels.run(str(claim.thread_id))

    assert result["status"] == "lease_lost"
    apply_mock.assert_not_called()


def test_sync_thread_labels_gmail_happy_path_records_and_persists_map(monkeypatch):
    claim = _claim_result()
    monkeypatch.setattr(tasks_label_sync, "claim_thread", lambda db, tid: claim)
    monkeypatch.setattr(tasks_label_sync, "set_pending_target", lambda *a, **k: True)
    _patch_passthrough_token_retry(monkeypatch)
    apply_result = service.GmailApplyResult(
        working_map={"Junk": "L1"}, add_ids=["L2"], remove_ids=[]
    )
    monkeypatch.setattr(tasks_label_sync, "apply_gmail_labels", lambda *a, **k: apply_result)
    persist_mock = MagicMock()
    monkeypatch.setattr(tasks_label_sync, "persist_gmail_label_map", persist_mock)
    monkeypatch.setattr(tasks_label_sync, "record_sync_result", lambda *a, **k: "recorded")

    result = tasks_label_sync.sync_thread_labels.run(str(claim.thread_id))

    assert result["status"] == "recorded"
    persist_mock.assert_called_once()
    assert persist_mock.call_args.kwargs["learned"] == apply_result.working_map


def test_sync_thread_labels_outlook_happy_path_skips_gmail_map_persist(monkeypatch):
    claim = _claim_result(
        account=service.AccountSnapshot(
            id=uuid4(),
            provider="outlook",
            access_token="at",
            refresh_token="rt",
            scope="Mail.ReadWrite",
            gmail_label_map=None,
        )
    )
    monkeypatch.setattr(tasks_label_sync, "claim_thread", lambda db, tid: claim)
    monkeypatch.setattr(tasks_label_sync, "set_pending_target", lambda *a, **k: True)
    _patch_passthrough_token_retry(monkeypatch)
    apply_mock = MagicMock()
    monkeypatch.setattr(tasks_label_sync, "apply_outlook_category", apply_mock)
    persist_mock = MagicMock()
    monkeypatch.setattr(tasks_label_sync, "persist_gmail_label_map", persist_mock)
    monkeypatch.setattr(tasks_label_sync, "record_sync_result", lambda *a, **k: "recorded")

    result = tasks_label_sync.sync_thread_labels.run(str(claim.thread_id))

    assert result["status"] == "recorded"
    apply_mock.assert_called_once()
    call_kwargs = apply_mock.call_args.kwargs
    assert call_kwargs["new_target"] == "m2"
    assert call_kwargs["previous_target"] is None
    persist_mock.assert_not_called()


def test_sync_thread_labels_invalid_grant_pauses(monkeypatch):
    # L2 regression: the task only catches `_AccountPaused` now, never a
    # bare `ValueError` -- `_token_retry` is the one that does the
    # classification, so this fakes ITS output rather than bypassing it.
    claim = _claim_result()
    monkeypatch.setattr(tasks_label_sync, "claim_thread", lambda db, tid: claim)
    monkeypatch.setattr(tasks_label_sync, "set_pending_target", lambda *a, **k: True)
    monkeypatch.setattr(
        tasks_label_sync,
        "_token_retry",
        lambda account: (
            lambda: MagicMock(),
            MagicMock(side_effect=tasks_label_sync._AccountPaused()),
        ),
    )

    result = tasks_label_sync.sync_thread_labels.run(str(claim.thread_id))

    assert result["status"] == "paused"


def test_sync_thread_labels_refresh_json_decode_error_is_error_not_paused(monkeypatch, caplog):
    # L2: a malformed 2xx token body raises json.JSONDecodeError, a
    # ValueError subclass -- but the account was never paused for it, so
    # this must land as a logged "error", not the silent "paused" a blanket
    # ValueError catch used to produce.
    claim = _claim_result()
    monkeypatch.setattr(tasks_label_sync, "claim_thread", lambda db, tid: claim)
    monkeypatch.setattr(tasks_label_sync, "set_pending_target", lambda *a, **k: True)
    monkeypatch.setattr(
        tasks_label_sync,
        "_refresh_gmail_access_token",
        MagicMock(side_effect=json.JSONDecodeError("bad body", "doc", 0)),
    )
    monkeypatch.setattr(tasks_label_sync, "_account_is_paused", lambda account_id: False)
    monkeypatch.setattr(
        tasks_label_sync, "apply_gmail_labels", MagicMock(side_effect=_http_error(401))
    )

    with caplog.at_level(logging.ERROR, logger="cortexmail"):
        result = tasks_label_sync.sync_thread_labels.run(str(claim.thread_id))

    assert result["status"] == "error"
    assert "ValueError but the account isn't paused" in caplog.text


def test_account_is_paused_reads_the_fresh_row(monkeypatch):
    account_id = uuid4()
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = datetime.now(timezone.utc)
    monkeypatch.setattr(tasks_label_sync, "SessionLocal", lambda: nullcontext(db))

    assert tasks_label_sync._account_is_paused(account_id) is True

    db.execute.return_value.scalar_one_or_none.return_value = None
    assert tasks_label_sync._account_is_paused(account_id) is False


def test_token_retry_refresh_gmail_invalid_grant_raises_account_paused(monkeypatch):
    account = service.AccountSnapshot(
        id=uuid4(),
        provider="gmail",
        access_token="at",
        refresh_token="rt",
        scope="https://www.googleapis.com/auth/gmail.modify",
        gmail_label_map=None,
    )
    monkeypatch.setattr(
        tasks_label_sync,
        "_refresh_gmail_access_token",
        MagicMock(side_effect=ValueError("Gmail authorization was revoked.")),
    )
    monkeypatch.setattr(tasks_label_sync, "_account_is_paused", lambda account_id: True)

    _get_client, with_retry = tasks_label_sync._token_retry(account)

    with pytest.raises(tasks_label_sync._AccountPaused):
        with_retry(lambda: (_ for _ in ()).throw(_http_error(401)))


def test_token_retry_refresh_reraises_a_non_pause_valueerror(monkeypatch, caplog):
    account = service.AccountSnapshot(
        id=uuid4(),
        provider="gmail",
        access_token="at",
        refresh_token="rt",
        scope="https://www.googleapis.com/auth/gmail.modify",
        gmail_label_map=None,
    )
    monkeypatch.setattr(
        tasks_label_sync,
        "_refresh_gmail_access_token",
        MagicMock(side_effect=json.JSONDecodeError("bad body", "doc", 0)),
    )
    monkeypatch.setattr(tasks_label_sync, "_account_is_paused", lambda account_id: False)

    _get_client, with_retry = tasks_label_sync._token_retry(account)

    with caplog.at_level(logging.ERROR, logger="cortexmail"):
        with pytest.raises(json.JSONDecodeError):
            with_retry(lambda: (_ for _ in ()).throw(_http_error(401)))

    assert "ValueError but the account isn't paused" in caplog.text


def test_sync_thread_labels_budget_exceeded(monkeypatch):
    claim = _claim_result()
    monkeypatch.setattr(tasks_label_sync, "claim_thread", lambda db, tid: claim)
    monkeypatch.setattr(tasks_label_sync, "set_pending_target", lambda *a, **k: True)
    _patch_passthrough_token_retry(monkeypatch)
    apply_mock = MagicMock()
    monkeypatch.setattr(tasks_label_sync, "apply_gmail_labels", apply_mock)
    # First call is the budget's own deadline stamp; the second is its
    # first check() call, far enough past the 60s budget to trip it.
    monkeypatch.setattr(tasks_label_sync, "monotonic", MagicMock(side_effect=[0, 1000]))

    result = tasks_label_sync.sync_thread_labels.run(str(claim.thread_id))

    assert result["status"] == "budget_exceeded"
    apply_mock.assert_not_called()


def test_sync_thread_labels_outlook_missing_etag_is_a_per_item_error(monkeypatch):
    # L3 end-to-end: MissingEtagError is neither a ValueError (no "paused"
    # misclassification, L2) nor swallowed -- it's a normal per-item
    # failure, logged and reported "error" so the next tick retries it.
    claim = _claim_result(
        account=service.AccountSnapshot(
            id=uuid4(),
            provider="outlook",
            access_token="at",
            refresh_token="rt",
            scope="mail.readwrite",
            gmail_label_map=None,
        ),
    )
    monkeypatch.setattr(tasks_label_sync, "claim_thread", lambda db, tid: claim)
    monkeypatch.setattr(tasks_label_sync, "set_pending_target", lambda *a, **k: True)
    _patch_passthrough_token_retry(monkeypatch)
    monkeypatch.setattr(
        tasks_label_sync,
        "apply_outlook_category",
        MagicMock(side_effect=outlook_client.MissingEtagError("m1")),
    )

    result = tasks_label_sync.sync_thread_labels.run(str(claim.thread_id))

    assert result["status"] == "error"


def test_sync_thread_labels_unexpected_error_is_caught(monkeypatch):
    claim = _claim_result()
    monkeypatch.setattr(tasks_label_sync, "claim_thread", lambda db, tid: claim)
    monkeypatch.setattr(tasks_label_sync, "set_pending_target", lambda *a, **k: True)
    _patch_passthrough_token_retry(monkeypatch)
    monkeypatch.setattr(
        tasks_label_sync,
        "apply_gmail_labels",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    result = tasks_label_sync.sync_thread_labels.run(str(claim.thread_id))

    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# label_sync_tick -- bounded per-account batch, per-item isolation.
# ---------------------------------------------------------------------------


def test_label_sync_tick_enqueues_and_isolates_per_account_failures(monkeypatch):
    good_account = uuid4()
    bad_account = uuid4()
    thread_ids = [uuid4(), uuid4()]

    db = MagicMock()
    account_result = MagicMock()
    account_result.scalars.return_value.all.return_value = [good_account, bad_account]
    good_threads_result = MagicMock()
    good_threads_result.scalars.return_value.all.return_value = thread_ids
    db.execute.side_effect = [account_result, good_threads_result, RuntimeError("boom")]

    monkeypatch.setattr(tasks_label_sync, "SessionLocal", lambda: nullcontext(db))
    delay_mock = MagicMock()
    monkeypatch.setattr(tasks_label_sync.sync_thread_labels, "delay", delay_mock)

    result = tasks_label_sync.label_sync_tick()

    assert result == {"accounts_swept": 2, "enqueued": 2, "failed": 1}
    assert delay_mock.call_count == 2
    db.rollback.assert_called_once()


def test_label_sync_tick_selects_only_enabled_unpaused_accounts():
    # SQL-shape check: the account-selection query filters on both flags.
    from sqlalchemy import select

    from app.db.models import ProviderAccount

    stmt = select(ProviderAccount.id).where(
        ProviderAccount.label_sync_enabled.is_(True),
        ProviderAccount.sync_paused_at.is_(None),
    )
    compiled = _compiled(stmt)
    assert "provider_account.label_sync_enabled IS true" in compiled
    assert "provider_account.sync_paused_at IS NULL" in compiled


# ---------------------------------------------------------------------------
# Beat/registration
# ---------------------------------------------------------------------------


def test_label_sync_tasks_are_registered_by_name():
    assert tasks_label_sync.sync_thread_labels.name == "app.workers.tasks_label_sync.sync_thread_labels"
    assert tasks_label_sync.label_sync_tick.name == "app.workers.tasks_label_sync.label_sync_tick"
    assert "app.workers.tasks_label_sync" in celery_app.conf.include


def test_label_sync_tick_beat_entry():
    entry = celery_app.conf.beat_schedule["label-sync-tick"]
    assert entry["task"] == "app.workers.tasks_label_sync.label_sync_tick"
    assert entry["schedule"] == 300.0
    assert entry["options"] == {"expires": 300}
