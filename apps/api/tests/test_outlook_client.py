from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from app.services.ingest import outlook_client
from app.services.ingest.outlook_client import (
    OUTLOOK_DELTA_CAP,
    DeltaExpiredError,
    OutlookClient,
)


def _resp(status_code=200, payload=None, headers=None):
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


def _fake_client(get_side_effect):
    fake = MagicMock()
    fake.get = MagicMock(side_effect=get_side_effect)
    return fake


def test_delta_cap_is_frozen_at_5000():
    assert OUTLOOK_DELTA_CAP == 5000


def test_get_me_hits_slash_me_without_prefer_header(monkeypatch):
    resp = _resp(200, {"mail": "a@example.com"})
    fake = _fake_client([resp])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    result = OutlookClient("tok").get_me()

    assert result == {"mail": "a@example.com"}
    call = fake.get.call_args
    assert call.args[0].endswith("/me")
    assert "Prefer" not in call.kwargs["headers"]


def test_get_message_returns_none_on_404(monkeypatch):
    resp = _resp(404)
    fake = _fake_client([resp])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    result = OutlookClient("tok").get_message("msg-1")

    assert result is None


def test_get_message_sends_immutable_id_prefer_and_select(monkeypatch):
    resp = _resp(200, {"id": "msg-1", "parentFolderId": "folder-1"})
    fake = _fake_client([resp])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    result = OutlookClient("tok").get_message("msg-1")

    assert result == {"id": "msg-1", "parentFolderId": "folder-1"}
    call = fake.get.call_args
    assert call.args[0].endswith("/me/messages/msg-1")
    assert call.kwargs["headers"]["Prefer"] == 'IdType="ImmutableId"'
    assert call.kwargs["params"] == {"$select": "id,parentFolderId"}


def test_delta_page_starts_fresh_walk_with_filter_and_select(monkeypatch):
    payload = {
        "value": [{"id": "m1", "conversationId": "c1"}],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-page",
    }
    resp = _resp(200, payload)
    fake = _fake_client([resp])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    result = OutlookClient("tok").delta_page(
        folder_key="inbox",
        received_after=datetime(2026, 1, 1, tzinfo=timezone.utc),
        page_size=25,
    )

    assert result == {
        "messages": [{"id": "m1", "conversationId": "c1"}],
        "removed_ids": [],
        "next_url": "https://graph.microsoft.com/v1.0/next-page",
        "delta_url": None,
    }
    call = fake.get.call_args
    assert call.args[0].endswith("/me/mailFolders/inbox/messages/delta")
    assert call.kwargs["headers"]["Prefer"] == 'IdType="ImmutableId"'
    params = call.kwargs["params"]
    assert params["$top"] == 25
    assert params["$filter"] == "receivedDateTime ge 2026-01-01T00:00:00Z"
    assert params["$select"] == (
        "id,conversationId,subject,from,toRecipients,ccRecipients,"
        "receivedDateTime,sentDateTime,bodyPreview,body,internetMessageId"
    )


def test_delta_page_follows_cursor_url_with_no_extra_params(monkeypatch):
    payload = {
        "value": [
            {"id": "m2", "conversationId": "c2"},
            {"id": "removed-1", "@removed": {"reason": "deleted"}},
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-final",
    }
    resp = _resp(200, payload)
    fake = _fake_client([resp])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    result = OutlookClient("tok").delta_page(cursor_url="https://graph.microsoft.com/v1.0/next-page")

    assert result["messages"] == [{"id": "m2", "conversationId": "c2"}]
    assert result["removed_ids"] == ["removed-1"]
    assert result["next_url"] is None
    assert result["delta_url"] == "https://graph.microsoft.com/v1.0/delta-final"
    call = fake.get.call_args
    assert call.args[0] == "https://graph.microsoft.com/v1.0/next-page"
    assert call.kwargs["params"] is None
    assert call.kwargs["headers"]["Prefer"] == 'IdType="ImmutableId"'


def test_delta_page_requires_folder_key_for_a_fresh_walk():
    with pytest.raises(ValueError):
        OutlookClient("tok").delta_page(cursor_url=None, folder_key=None)


def test_delta_page_raises_expired_on_410(monkeypatch):
    resp = _resp(410)
    fake = _fake_client([resp])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    with pytest.raises(DeltaExpiredError):
        OutlookClient("tok").delta_page(folder_key="inbox")


@pytest.mark.parametrize(
    "code", ["SyncStateNotFound", "resyncrequired", "SyncStateInvalid", "SYNCSTATEINVALID"]
)
def test_delta_page_raises_expired_on_4xx_error_codes_case_insensitive(monkeypatch, code):
    resp = _resp(400, {"error": {"code": code}})
    fake = _fake_client([resp])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    with pytest.raises(DeltaExpiredError):
        OutlookClient("tok").delta_page(folder_key="inbox")


def test_delta_page_other_4xx_codes_surface_as_http_errors_not_expiry(monkeypatch):
    resp = _resp(400, {"error": {"code": "InvalidRequest"}})
    fake = _fake_client([resp])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    with pytest.raises(httpx.HTTPStatusError):
        OutlookClient("tok").delta_page(folder_key="inbox")


def test_delta_page_non_json_4xx_body_is_not_mistaken_for_expiry(monkeypatch):
    resp = _resp(400)
    resp.json.side_effect = ValueError("not json")
    fake = _fake_client([resp])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    with pytest.raises(httpx.HTTPStatusError):
        OutlookClient("tok").delta_page(folder_key="inbox")


def test_429_is_retried_honoring_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(outlook_client.time, "sleep", lambda s: slept.append(s))
    throttled = _resp(429, headers={"Retry-After": "3"})
    ok = _resp(200, {"value": [], "@odata.deltaLink": "https://x/delta"})
    fake = _fake_client([throttled, ok])
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    result = OutlookClient("tok").delta_page(folder_key="inbox")

    assert result["delta_url"] == "https://x/delta"
    assert slept == [3.0]
    assert fake.get.call_count == 2


def test_5xx_retries_are_bounded_then_raise(monkeypatch):
    monkeypatch.setattr(outlook_client.time, "sleep", lambda s: None)
    failing = [_resp(503), _resp(502), _resp(500)]
    fake = _fake_client(failing)
    monkeypatch.setattr(outlook_client, "_client", lambda: fake)

    with pytest.raises(httpx.HTTPStatusError):
        OutlookClient("tok").delta_page(folder_key="inbox")

    assert fake.get.call_count == 3
