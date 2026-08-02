"""Shared OpenAI-compatible chat-completions wire call.

Extraction and classification both need the same call -- a bearer-key POST
to a preset or user-supplied (`provider="custom"`) endpoint -- with only the
prompt, content, and response parsing differing. This module owns the wire
call and every security rule around it; callers own their own prompt and
parsing.
"""

from __future__ import annotations

import json

import httpx

from app.core.logging import logger
from app.services.nlp.providers import DestinationRejected, LlmCredential, pin_custom_destination


class LlmCallError(Exception):
    """
    The single internal failure carrier for `call_chat_completion`: every
    failure mode -- destination policy, connection, non-2xx, or a malformed
    response shape -- raises this instead of letting an unrelated exception
    type escape. `category` is one of connection_failed | http_<status> |
    invalid_response | blocked_by_policy; `status` is the HTTP status code
    when one exists, else `None`. Callers map this to their own result
    contract -- extraction's unchanged `None`, `/test`'s category set,
    classification's heuristic fallback -- none derives a category from a
    lossy `None`.
    """

    def __init__(self, category: str, status: int | None) -> None:
        self.category = category
        self.status = status
        super().__init__(category)


def _raise_invalid_response(provider: str, status: int) -> None:
    logger.warning("LLM call returned a malformed response shape for provider %s", provider)
    raise LlmCallError("invalid_response", status)


def call_chat_completion(
    credential: LlmCredential, *, prompt: str, user_content: str, max_tokens: int
) -> str:
    """
    Perform the OpenAI-compatible chat-completions call and return the raw
    `choices[0].message.content` string. Raises `LlmCallError` on every
    failure -- the single internal failure carrier.

    For `provider="custom"` the destination policy is re-run and PINNED
    FIRST, immediately before this specific request: a sweep resolves
    credentials once but can run for minutes, so a DNS answer or a flag
    flipping non-global between two calls must block the second one before
    any request leaves. Re-validating and then handing httpx the same
    hostname would still leave a TOCTOU window -- httpx does its own DNS
    lookup, and a hostname can answer public for the check and private for
    that lookup microseconds later. `pin_custom_destination` closes it: we
    connect to the exact address just validated, so httpx never resolves
    the hostname itself.
    """
    pinned = None
    if credential.provider == "custom":
        try:
            pinned = pin_custom_destination(credential.base_url)
        except DestinationRejected as exc:
            raise LlmCallError("blocked_by_policy", None) from exc

    body = {
        "model": credential.model,
        "messages": [{"role": "user", "content": f"{prompt}\n\n{user_content}"}],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }

    if pinned is not None:
        url = f"{pinned.url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {credential.api_key}",
            # The server on the other end still needs the hostname it was
            # configured with, even though we're connecting to its IP.
            "Host": pinned.host_header,
        }
        # None for http, or when the host was already an IP literal -- the
        # SNI extension of the TLS ClientHello is meaningless there, and
        # httpx would otherwise send the bare IP as both SNI and the
        # hostname it verifies the certificate against.
        extensions = {"sni_hostname": pinned.sni_hostname} if pinned.sni_hostname else None
    else:
        url = f"{credential.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {credential.api_key}"}
        extensions = None

    # trust_env=False is mandatory: httpx otherwise honors HTTP(S)_PROXY/
    # ALL_PROXY env vars and would route the bearer credential through a
    # proxy address the destination policy never validated. Redirects are
    # never followed (httpx's default) -- a 3xx is a call failure, not
    # something to chase, but `raise_for_status()` alone only raises on
    # 4xx/5xx, so a 3xx (never followed) would otherwise fall through to
    # `response.json()` and surface as `invalid_response` instead of the
    # `http_<status>` it actually is. Check `is_success` explicitly first.
    try:
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            response = client.post(
                url,
                headers=headers,
                json=body,
                extensions=extensions,
            )
            if not response.is_success:
                status = response.status_code
                logger.warning(
                    "LLM call failed for provider %s: http_%s",
                    credential.provider, status,
                )
                raise LlmCallError(f"http_{status}", status)
    except httpx.HTTPError as exc:
        logger.warning(
            "LLM call failed for provider %s: %s",
            credential.provider, type(exc).__name__,
        )
        raise LlmCallError("connection_failed", None) from exc

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        logger.warning(
            "LLM call returned unparseable JSON for provider %s: %s",
            credential.provider, type(exc).__name__,
        )
        raise LlmCallError("invalid_response", response.status_code) from exc

    # Explicit shape checks -- never let an uncaught IndexError/KeyError/
    # TypeError leak out of a malformed but "successful" reply.
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        _raise_invalid_response(credential.provider, response.status_code)

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        _raise_invalid_response(credential.provider, response.status_code)

    content = message.get("content")
    if not isinstance(content, str):
        _raise_invalid_response(credential.provider, response.status_code)

    return content
