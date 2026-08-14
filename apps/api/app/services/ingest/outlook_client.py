"""Thin Microsoft Graph client for the Outlook mail delta walk.

Mirrors gmail_client.py's pooling and retry shape -- same shared, lazily-built
httpx.Client, same bounded-attempt loop -- swapped for Graph's quirks: an
immutable-id preference header on every message request, and delta-walk
expiry that Graph signals two different ways (a bare 410, or a 4xx body
whose error code names the stale sync state).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx

from app.core.logging import logger

_BASE_URL = "https://graph.microsoft.com/v1.0"
_TIMEOUT = 20.0
_MAX_ATTEMPTS = 3
_RETRY_5XX = frozenset({500, 502, 503, 504})
# Label sync's task deadline is 8 minutes and its claim lease is 10 (see
# app/workers/tasks_label_sync.py) -- `_get`'s ingest-path Retry-After
# compliance is deliberately unbounded (a long delta walk can afford to
# wait), but a label-sync call honoring a large Retry-After verbatim could
# sleep past a STOLEN lease and keep writing after another task has taken
# over. The label-path methods below cap any single sleep at this many
# seconds; a server-requested wait beyond it fails the attempt immediately
# (no partial sleep) instead of blocking, so a bad item fails cleanly and
# the tick's per-item isolation retries it later.
_LABEL_SYNC_MAX_RETRY_SLEEP = 30.0
_PREFER_IMMUTABLE_ID = 'IdType="ImmutableId"'
_MESSAGE_SELECT = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,"
    "receivedDateTime,sentDateTime,bodyPreview,body,internetMessageId"
)
# Graph's error codes for a delta cursor that's aged out or been invalidated
# server-side -- casing isn't documented as stable, so compare lowercased.
_EXPIRED_CODES = frozenset({"syncstatenotfound", "resyncrequired", "syncstateinvalid"})

# Graph's filtered delta walk (the $filter=receivedDateTime path we use for
# the baseline) truncates silently past this many results -- there is no
# overflow error. The ingest layer counts messages itself and re-baselines
# with a narrower window when it suspects truncation.
OUTLOOK_DELTA_CAP = 5000

_http: httpx.Client | None = None


def _client() -> httpx.Client:
    """Build the pooled client on first use, so importing this module never
    opens sockets (the offline test suite imports it freely)."""
    global _http
    if _http is None:
        _http = httpx.Client(
            base_url=_BASE_URL,
            timeout=_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _http


class DeltaExpiredError(Exception):
    """A folder's delta cursor is no longer honored by Graph -- the caller
    must start a fresh baseline generation for that folder."""


class MissingEtagError(Exception):
    """Graph's $select=categories response omitted @odata.etag -- rare, but
    passing `None` straight into an If-Match header would fail inside httpx
    with a confusing TypeError instead of a clear, catchable signal.
    Deliberately NOT a ValueError: label sync's token-refresh path gives
    ValueError a specific "account paused" meaning
    (app/workers/tasks_label_sync.py), and this has nothing to do with
    that -- it's a normal per-item provider failure, retried next tick."""

    def __init__(self, message_id: str):
        super().__init__(f"Outlook message {message_id} has no @odata.etag")
        self.message_id = message_id


def _graph_error_code(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    return str((body.get("error") or {}).get("code") or "").lower()


class OutlookClient:
    def __init__(self, token: str):
        self.token = token

    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = {"Authorization": f"Bearer {self.token}"}
        if headers:
            request_headers.update(headers)
        resp: httpx.Response | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            resp = _client().get(url, headers=request_headers, params=params)
            if resp.status_code == 429 and attempt < _MAX_ATTEMPTS:
                retry_after = resp.headers.get("Retry-After")
                # Graph sends integer seconds, but Retry-After can legally be
                # an HTTP-date (e.g. from an intermediary) -- fall back to
                # backoff rather than crashing the run on float().
                try:
                    wait = float(retry_after) if retry_after else float(2 ** (attempt - 1))
                except ValueError:
                    wait = float(2 ** (attempt - 1))
                logger.warning("Graph 429 for %s; retrying in %ss", url, wait)
                time.sleep(wait)
                continue
            if resp.status_code in _RETRY_5XX and attempt < _MAX_ATTEMPTS:
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "Graph %s returned %s; retrying in %ss", url, resp.status_code, backoff
                )
                time.sleep(backoff)
                continue
            break
        assert resp is not None  # pragma: no cover
        return resp

    def _get_bounded(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Label-sync-only GET retry loop -- same shape as `_get`, but every
        single sleep (429 Retry-After OR 5xx backoff) is capped at
        `_LABEL_SYNC_MAX_RETRY_SLEEP`. Kept as its OWN method rather than a
        parameter on `_get` so ingest's unbounded Retry-After compliance
        (a deliberate, different tradeoff for a long delta walk) can never
        be changed by a label-sync edit.
        """
        request_headers = {"Authorization": f"Bearer {self.token}"}
        if headers:
            request_headers.update(headers)
        resp: httpx.Response | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            resp = _client().get(url, headers=request_headers, params=params)
            if resp.status_code == 429 and attempt < _MAX_ATTEMPTS:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else float(2 ** (attempt - 1))
                except ValueError:
                    wait = float(2 ** (attempt - 1))
                if wait > _LABEL_SYNC_MAX_RETRY_SLEEP:
                    logger.warning(
                        "Graph 429 for %s requested a %ss wait, over the label-sync "
                        "cap (%ss) -- failing this attempt instead of sleeping",
                        url,
                        wait,
                        _LABEL_SYNC_MAX_RETRY_SLEEP,
                    )
                    break
                logger.warning("Graph 429 for %s; retrying in %ss", url, wait)
                time.sleep(wait)
                continue
            if resp.status_code in _RETRY_5XX and attempt < _MAX_ATTEMPTS:
                backoff = min(float(2 ** (attempt - 1)), _LABEL_SYNC_MAX_RETRY_SLEEP)
                logger.warning(
                    "Graph %s returned %s; retrying in %ss", url, resp.status_code, backoff
                )
                time.sleep(backoff)
                continue
            break
        assert resp is not None  # pragma: no cover
        return resp

    def _post(
        self,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """POST for reply-send calls. No retry loop at all -- unlike `_get`'s
        429/5xx retries (safe for idempotent reads), a blind retry of a send
        or a draft-mutating call risks a double-send or a duplicate draft.
        The caller (mail_send/outlook_send.py) treats any failure here as an
        ambiguous outcome, never something to replay itself.
        """
        request_headers = {"Authorization": f"Bearer {self.token}"}
        if headers:
            request_headers.update(headers)
        return _client().post(url, headers=request_headers, json=json_body)

    def create_reply(self, message_id: str, *, reply_all: bool, comment: str) -> dict[str, Any]:
        """POST /me/messages/{id}/createReply (or createReplyAll), with the
        immutable-id Prefer header so the returned draft id survives the
        draft -> Sent Items move (plan §3.1). Graph computes recipients from
        the original message's Reply-To/To/Cc; the comment path preserves
        the quoted original.
        """
        segment = "createReplyAll" if reply_all else "createReply"
        resp = self._post(
            f"{_BASE_URL}/me/messages/{message_id}/{segment}",
            json_body={"comment": comment},
            headers={"Prefer": _PREFER_IMMUTABLE_ID},
        )
        resp.raise_for_status()
        return resp.json()

    def send_draft(self, draft_id: str) -> None:
        """POST /me/messages/{draft_id}/send -- no request body, no response
        body on success."""
        resp = self._post(
            f"{_BASE_URL}/me/messages/{draft_id}/send",
            headers={"Prefer": _PREFER_IMMUTABLE_ID},
        )
        resp.raise_for_status()

    def delete_draft(self, draft_id: str) -> None:
        """DELETE /me/messages/{draft_id} -- used to clean up a reply draft
        that fails the recipient cap after createReply computed it (plan
        §3.3)."""
        headers = {"Authorization": f"Bearer {self.token}", "Prefer": _PREFER_IMMUTABLE_ID}
        resp = _client().request("DELETE", f"{_BASE_URL}/me/messages/{draft_id}", headers=headers)
        resp.raise_for_status()

    def get_me(self) -> dict[str, Any]:
        resp = self._get(f"{_BASE_URL}/me")
        resp.raise_for_status()
        return resp.json()

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        """GET /me/messages/{id} with $select=id,parentFolderId.

        Returns None on 404 (message truly gone).
        """
        resp = self._get(
            f"{_BASE_URL}/me/messages/{message_id}",
            params={"$select": "id,parentFolderId"},
            headers={"Prefer": _PREFER_IMMUTABLE_ID},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_message_categories(self, message_id: str) -> dict[str, Any] | None:
        """GET /me/messages/{id}?$select=categories, reading BOTH the
        categories and the @odata.etag -- label sync's If-Match PATCH below
        needs the etag to avoid clobbering a category the user adds between
        this read and that write (label-sync plan §3.2). Returns None on
        404, same "message truly gone" contract as get_message.

        Raises MissingEtagError if Graph's response omits @odata.etag (rare
        but documented as possible) -- better a clear, catchable exception
        here than a None etag reaching the If-Match header and blowing up
        inside httpx.
        """
        resp = self._get_bounded(
            f"{_BASE_URL}/me/messages/{message_id}",
            params={"$select": "categories"},
            headers={"Prefer": _PREFER_IMMUTABLE_ID},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        etag = body.get("@odata.etag")
        if etag is None:
            raise MissingEtagError(message_id)
        return {"categories": body.get("categories") or [], "etag": etag}

    def set_message_categories(
        self, message_id: str, categories: list[str], *, etag: str
    ) -> httpx.Response:
        """PATCH /me/messages/{id} with an If-Match etag -- Graph's
        categories PATCH replaces the WHOLE collection, so the etag is what
        makes a 412 (rather than a silent overwrite) the caller's signal to
        re-GET and re-merge once (label-sync plan §3.2).

        Retry-safe the same way `_get` is (429/5xx, idempotent replace of a
        fixed list) -- but a 412 is returned to the caller, not retried
        here, since recovering from it needs a fresh GET this method
        doesn't have. The raw response is returned (not raise_for_status'd)
        so the caller can branch on 404/412 without exception-based control
        flow.

        Every single sleep here is capped at `_LABEL_SYNC_MAX_RETRY_SLEEP`
        (plan §3.1 P4-2) -- a Retry-After beyond that fails the attempt
        immediately (no partial sleep) rather than risking a live write
        outliving a stolen claim lease; the caller's per-item isolation
        retries it on a later tick.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Prefer": _PREFER_IMMUTABLE_ID,
            "If-Match": etag,
        }
        url = f"{_BASE_URL}/me/messages/{message_id}"
        resp: httpx.Response | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            resp = _client().patch(url, headers=headers, json={"categories": categories})
            if resp.status_code == 429 and attempt < _MAX_ATTEMPTS:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else float(2 ** (attempt - 1))
                except ValueError:
                    wait = float(2 ** (attempt - 1))
                if wait > _LABEL_SYNC_MAX_RETRY_SLEEP:
                    logger.warning(
                        "Graph 429 for %s requested a %ss wait, over the label-sync "
                        "cap (%ss) -- failing this attempt instead of sleeping",
                        url,
                        wait,
                        _LABEL_SYNC_MAX_RETRY_SLEEP,
                    )
                    break
                logger.warning("Graph 429 for %s; retrying in %ss", url, wait)
                time.sleep(wait)
                continue
            if resp.status_code in _RETRY_5XX and attempt < _MAX_ATTEMPTS:
                backoff = min(float(2 ** (attempt - 1)), _LABEL_SYNC_MAX_RETRY_SLEEP)
                logger.warning("Graph %s returned %s; retrying in %ss", url, resp.status_code, backoff)
                time.sleep(backoff)
                continue
            break
        assert resp is not None  # pragma: no cover
        return resp

    def delta_page(
        self,
        *,
        folder_key: str | None = None,
        cursor_url: str | None = None,
        received_after: datetime | None = None,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """One page of a messages delta walk: follows cursor_url, or starts a
        fresh walk on /me/mailFolders/{folder_key}/messages/delta with
        $filter=receivedDateTime ge {received_after}.

        Returns {"messages": [...], "removed_ids": [...],
                 "next_url": str | None, "delta_url": str | None}
        (exactly one of next_url/delta_url set).

        Raises DeltaExpiredError on HTTP 410 OR 4xx bodies whose error code is
        SyncStateNotFound / ResyncRequired / SyncStateInvalid
        (case-insensitive). There is NO overflow error -- Graph truncates
        filtered walks silently; cap detection is the ingest layer's job.
        """
        if cursor_url:
            resp = self._get(cursor_url, headers={"Prefer": _PREFER_IMMUTABLE_ID})
        else:
            if not folder_key:
                raise ValueError("folder_key is required to start a fresh delta walk")
            params: dict[str, Any] = {"$select": _MESSAGE_SELECT, "$top": page_size}
            if received_after is not None:
                params["$filter"] = (
                    f"receivedDateTime ge {received_after.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                )
            resp = self._get(
                f"{_BASE_URL}/me/mailFolders/{folder_key}/messages/delta",
                params=params,
                headers={"Prefer": _PREFER_IMMUTABLE_ID},
            )

        if resp.status_code == 410:
            raise DeltaExpiredError(f"delta cursor expired (410) for folder={folder_key!r}")
        if 400 <= resp.status_code < 500:
            code = _graph_error_code(resp)
            if code in _EXPIRED_CODES:
                raise DeltaExpiredError(
                    f"delta cursor expired ({code}) for folder={folder_key!r}"
                )

        resp.raise_for_status()
        payload = resp.json()

        messages: list[dict[str, Any]] = []
        removed_ids: list[str] = []
        for item in payload.get("value", []):
            if "@removed" in item:
                if item.get("id"):
                    removed_ids.append(item["id"])
            else:
                messages.append(item)

        return {
            "messages": messages,
            "removed_ids": removed_ids,
            "next_url": payload.get("@odata.nextLink"),
            "delta_url": payload.get("@odata.deltaLink"),
        }
