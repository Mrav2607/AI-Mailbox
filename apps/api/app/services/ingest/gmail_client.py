from __future__ import annotations

from typing import Any

import httpx


class GmailClient:
    def __init__(self, token: str):
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def list_messages(self, max_results: int = 50, page_token: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        resp = httpx.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=self._headers(),
            params=params,
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()

    def get_message(self, message_id: str) -> dict[str, Any]:
        resp = httpx.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            headers=self._headers(),
            params={"format": "full"},
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()

    def list_threads(self, max_results: int = 50, page_token: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        resp = httpx.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/threads",
            headers=self._headers(),
            params=params,
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        # format=full returns every message in the thread with headers + body,
        # each shaped exactly like a messages.get response (so normalize_message
        # works on them unchanged).
        resp = httpx.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
            headers=self._headers(),
            params={"format": "full"},
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()

    def get_profile(self) -> dict[str, Any]:
        resp = httpx.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers=self._headers(),
            timeout=20.0,
        )
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
        resp = httpx.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/history",
            headers=self._headers(),
            params=params,
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()
