from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup


def _decode_base64url(data: str | None) -> str | None:
    if not data:
        return None
    padded = data.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_headers(headers: list[dict[str, Any]]) -> dict[str, str]:
    return {h.get("name", ""): h.get("value", "") for h in headers if h.get("name")}


def _html_to_text(html: str) -> str | None:
    """Plain-text fallback for HTML-only messages, so body_text is never None
    when any body exists (it feeds the classifier and the UI's text view)."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return None
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    # The separator puts a newline between any two text nodes, which already
    # covers <br> and block boundaries.
    text = "\n".join(line.strip() for line in soup.get_text(separator="\n").splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _extract_body(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """First text/plain and text/html found anywhere in the MIME tree.

    Gmail nests bodies (multipart/mixed -> multipart/alternative -> text/*),
    so walk depth-first instead of only scanning the top level. Parts with a
    filename are attachments, not bodies."""
    text_body: str | None = None
    html_body: str | None = None

    def walk(part: dict[str, Any]) -> None:
        nonlocal text_body, html_body
        if not part or (text_body is not None and html_body is not None):
            return
        if part.get("filename"):
            return
        mime_type = part.get("mimeType")
        data = part.get("body", {}).get("data")
        if mime_type == "text/plain" and text_body is None:
            text_body = _decode_base64url(data)
        elif mime_type == "text/html" and html_body is None:
            html_body = _decode_base64url(data)
        for child in part.get("parts", []) or []:
            walk(child)

    if payload:
        walk(payload)
    if text_body is None and html_body:
        text_body = _html_to_text(html_body)
    return text_body, html_body


def normalize_message(raw: dict[str, Any]) -> dict[str, Any]:
    headers = _extract_headers(raw.get("payload", {}).get("headers", []))
    subject = headers.get("Subject")
    sender = headers.get("From")
    to_list = [addr.strip() for addr in headers.get("To", "").split(",") if addr.strip()]
    cc_list = [addr.strip() for addr in headers.get("Cc", "").split(",") if addr.strip()]
    bcc_list = [addr.strip() for addr in headers.get("Bcc", "").split(",") if addr.strip()]
    text_body, html_body = _extract_body(raw.get("payload", {}))

    internal_ms = raw.get("internalDate")
    sent_at = None
    if internal_ms:
        sent_at = datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)

    return {
        "provider_message_id": raw.get("id"),
        "provider_thread_id": raw.get("threadId"),
        "snippet": raw.get("snippet"),
        "subject": subject,
        "sender": sender,
        "recipient": to_list or None,
        "cc": cc_list or None,
        "bcc": bcc_list or None,
        "sent_at": sent_at,
        "body_text": text_body,
        "body_html": html_body,
        "headers": headers,
        # Retained for ingest policy decisions (not persisted yet). Gmail's
        # SENT label is more reliable than parsing display-name email headers
        # when deciding whether a new message is inbound.
        "label_ids": raw.get("labelIds", []),
    }
