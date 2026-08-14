"""Thin Gmail REST client.

Every call used to open its own connection, which meant a fresh TCP + TLS
handshake per request -- on a 500-thread pull that's 500 handshakes on the
slowest path in the app. The client below is shared process-wide (auth rides on
each request's headers, so nothing about it is user-specific) and pools its
connections.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.logging import logger

_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
_TIMEOUT = 20.0
# Gmail rate-limits per user and occasionally 5xxs. Retry a few times with
# backoff rather than losing a long pull to one blip.
_MAX_ATTEMPTS = 3
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# A send is not idempotent the way a GET pull is -- a blind 5xx/timeout retry
# risks a double-send, so send_message only ever retries a 429 (a definite
# "not yet accepted" signal), bounded the same way as the GET path.
_SEND_MAX_ATTEMPTS = 3

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


class GmailClient:
    def __init__(self, token: str):
        self.token = token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            resp = _client().get(path, headers=headers, params=params)
            # A 401 is the caller's cue to refresh the token, so it goes straight
            # through -- retrying it here would just burn the attempts.
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "Gmail %s returned %s; retrying in %ss", path, resp.status_code, backoff
                )
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def list_threads(self, max_results: int = 50, page_token: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return self._get("/threads", params)

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        # format=full returns every message in the thread with headers + body,
        # each shaped exactly like a messages.get response (so normalize_message
        # works on them unchanged).
        return self._get(f"/threads/{thread_id}", {"format": "full"})

    def get_profile(self) -> dict[str, Any]:
        return self._get("/profile")

    def _post(self, path: str, json_body: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"}
        return _client().post(path, headers=headers, json=json_body)

    def send_message(self, *, raw: str, thread_id: str) -> dict[str, Any]:
        """POST /messages/send with a base64url-encoded raw MIME message and
        the thread to append it to. Bounded 429 backoff only -- everything
        else (including 5xx) surfaces to the caller as an ambiguous outcome
        (plan §3.6 step 4): blindly retrying a send we can't confirm failed
        is exactly the double-send risk this feature exists to avoid.
        """
        body: dict[str, Any] = {"raw": raw, "threadId": thread_id}
        resp = self._post("/messages/send", body)
        for attempt in range(1, _SEND_MAX_ATTEMPTS):
            if resp.status_code != 429:
                break
            backoff = 2 ** (attempt - 1)
            logger.warning("Gmail send 429; retrying in %ss", backoff)
            time.sleep(backoff)
            resp = self._post("/messages/send", body)
        resp.raise_for_status()
        return resp.json()

    def list_history(
        self,
        start_history_id: str,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "maxResults": 500,
        }
        if page_token:
            params["pageToken"] = page_token
        return self._get("/history", params)
