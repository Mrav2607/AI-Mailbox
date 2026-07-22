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
