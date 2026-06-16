from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any


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


def _extract_body(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    if not payload:
        return None, None
    mime_type = payload.get("mimeType")
    body_data = payload.get("body", {}).get("data")
    if mime_type == "text/plain":
        return _decode_base64url(body_data), None
    if mime_type == "text/html":
        return None, _decode_base64url(body_data)

    text_body = None
    html_body = None
    for part in payload.get("parts", []) or []:
        part_type = part.get("mimeType")
        part_data = part.get("body", {}).get("data")
        if part_type == "text/plain" and text_body is None:
            text_body = _decode_base64url(part_data)
        elif part_type == "text/html" and html_body is None:
            html_body = _decode_base64url(part_data)
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
    }
