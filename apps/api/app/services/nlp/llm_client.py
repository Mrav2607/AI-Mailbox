"""Shared OpenAI-compatible chat-completions wire call.

Extraction and classification both need the same call -- a bearer-key POST
to a preset or user-supplied (`provider="custom"`) endpoint -- with only the
prompt, content, and response parsing differing. This module owns the wire
call and every security rule around it; callers own their own prompt and
parsing.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import NoReturn

import httpx

from app.core.logging import logger
from app.services.nlp.providers import DestinationRejected, LlmCredential, pin_custom_destination


@dataclass(frozen=True)
class LlmUsage:
    """
    Token counts a provider reported for one call, each field independently
    optional -- a provider can send `prompt_tokens` and omit `total_tokens`,
    and we must not paper over that by adding the parts we do have. See
    `_parse_usage` for why `total_tokens` is never synthesized.
    """

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class LlmCallResult:
    """What `call_chat_completion` hands back: the parsed content string
    every caller already expects, plus whatever usage telemetry came with it
    (`None` when the provider sent nothing usable)."""

    content: str
    usage: LlmUsage | None


class LlmCallError(Exception):
    """
    The single internal failure carrier for `call_chat_completion`: every
    failure mode -- destination policy, connection, non-2xx, or a malformed
    response shape -- raises this instead of letting an unrelated exception
    type escape. `category` is one of connection_failed | timed_out |
    http_<status> | invalid_response | blocked_by_policy; `status` is the HTTP
    status code when one exists, else `None`. Callers map this to their own
    result contract -- extraction's unchanged `None`, `/test`'s category set,
    classification's heuristic fallback -- none derives a category from a
    lossy `None`.
    """

    def __init__(self, category: str, status: int | None) -> None:
        self.category = category
        self.status = status
        super().__init__(category)


# Categories raised BEFORE any request leaves the process -- currently only
# the destination-policy rejection (`pin_custom_destination` below), which
# runs and can reject before the request body is even built. This matters to
# callers tracking `llm_attempted` (plan: 2026-08-14-llm-failure-visibility):
# a future spend/quota surface reading that flag would overcount billable
# calls if a preflight refusal counted as "issued". `timed_out` is only raised
# once a response has started coming back, so it really was issued.
#
# `connection_failed` is the honest grey area: it catches httpx.HTTPError
# wholesale, which spans "DNS never resolved" (nothing sent, nothing billable)
# and "connection dropped mid-response" (sent, possibly billed). It counts as
# issued deliberately -- for a feature whose job is surfacing failures,
# over-counting an attempt is safer than quietly under-counting one. Split it
# only if a spend/quota surface ever needs the distinction.
PREFLIGHT_CATEGORIES = frozenset({"blocked_by_policy"})


def request_was_issued(category: str) -> bool:
    """Whether an `LlmCallError` of this `category` means a request actually
    left the process -- see `PREFLIGHT_CATEGORIES` for why `blocked_by_policy`
    is the one exception."""
    return category not in PREFLIGHT_CATEGORIES


# A TCP connect slower than this is a dead host no matter how slow the model
# behind it is, so it gets its own cap well inside the overall budget.
_CONNECT_TIMEOUT_S = 5.0


def _raise_invalid_response(provider: str, status: int) -> NoReturn:
    logger.warning("LLM call returned a malformed response shape for provider %s", provider)
    raise LlmCallError("invalid_response", status)


def _raise_timed_out(provider: str) -> NoReturn:
    logger.warning("LLM call exceeded its time budget for provider %s", provider)
    raise LlmCallError("timed_out", None)


def _read_body_within_deadline(response: httpx.Response, deadline: float, provider: str) -> bytes:
    """
    Read the whole body under a wall-clock deadline and return the raw bytes.

    httpx's `read` timeout bounds a SINGLE socket read, not the request. An
    endpoint that dribbles one byte just under that timeout, forever, resets
    it on every chunk and the call never returns -- which on the
    classification path means one message can hold an ingest worker open
    indefinitely. Two things bound it here: the deadline is re-checked as
    each chunk lands, and a watchdog closes the response the moment it
    passes. That close is what actually interrupts a read already blocked on
    a silent socket; without it we'd still sit through one more full `read`
    timeout before noticing.

    Raises `LlmCallError("timed_out", None)` once the deadline passes. Any
    other transport failure is re-raised untouched for the caller to map.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _raise_timed_out(provider)

    expired = threading.Event()

    def _abort() -> None:
        # Set the flag BEFORE closing: the close is what makes the blocked
        # read raise, and the flag is how we recognize that raise as ours.
        expired.set()
        response.close()

    watchdog = threading.Timer(remaining, _abort)
    watchdog.daemon = True
    watchdog.start()

    chunks: list[bytes] = []
    try:
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                expired.set()
                break
            chunks.append(chunk)
    except Exception:
        # Broad on purpose, then narrowed immediately: our own watchdog
        # closing the socket mid-read surfaces as whatever the transport
        # happened to be doing (httpx raises StreamError off RuntimeError,
        # not HTTPError), so `expired` is the only reliable way to tell that
        # apart from a real failure. Everything else re-raises unchanged, so
        # a genuine bug still escapes instead of being logged as a timeout.
        if expired.is_set():
            _raise_timed_out(provider)
        raise
    finally:
        watchdog.cancel()

    if expired.is_set():
        _raise_timed_out(provider)

    return b"".join(chunks)


def _parse_token_count(value: object) -> int | None:
    """
    Accept a field only if it's a real, non-negative `int`. `bool` is a
    subclass of `int` in Python (`isinstance(True, int)` is `True`), so it's
    excluded explicitly -- otherwise a provider (or a fuzzer) sending
    `"prompt_tokens": true` would silently count as one token. A negative
    count is nonsensical and treated the same as absent, not clamped.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _parse_usage(payload: object) -> LlmUsage | None:
    """
    Pull token counts out of `payload["usage"]`, as defensively as possible.

    This can NEVER raise and never synthesizes a missing field -- usage is
    telemetry riding along on a successful response, not part of the
    contract that response has to satisfy. A malformed or absent `usage`
    block must still let an otherwise-good call return its content
    normally, so every shape failure here just means "count this field (or
    the whole block) as absent" rather than an error.

    In particular, `total_tokens` is never computed as `prompt_tokens +
    completion_tokens` when the provider omits it: the plan tracks "calls
    that reported a usable total" as its own counter, and a total we made up
    ourselves would corrupt that count with numbers no provider actually
    reported.
    """
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None

    prompt_tokens = _parse_token_count(usage.get("prompt_tokens"))
    completion_tokens = _parse_token_count(usage.get("completion_tokens"))
    total_tokens = _parse_token_count(usage.get("total_tokens"))

    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return None

    return LlmUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def call_chat_completion(
    credential: LlmCredential,
    *,
    prompt: str,
    user_content: str,
    max_tokens: int,
    timeout: float = 30.0,
) -> LlmCallResult:
    """
    Perform the OpenAI-compatible chat-completions call and return an
    `LlmCallResult` carrying the raw `choices[0].message.content` string plus
    whatever usage telemetry the provider included. Raises `LlmCallError` on
    every failure -- the single internal failure carrier. A malformed
    `usage` block is never one of those failures; see `_parse_usage`.

    `timeout` is a TOTAL WALL-CLOCK budget for the complete call -- DNS and
    destination pinning, connect, send, and reading the entire body -- not a
    per-operation timeout. httpx's own timeouts are per-operation, so an
    endpoint trickling bytes just under the `read` timeout would otherwise
    reset it forever and never return. The deadline is enforced while the
    body streams in and the response is closed the moment it passes, so the
    call is cancelled rather than merely reported late. Connect is
    additionally capped at 5s inside that budget: a TCP connect slower than
    that is a dead host no matter how slow the model itself might be, and
    failing it early is what keeps an unreachable endpoint cheap without
    punishing a legitimately slow generation. Blowing the budget raises
    `LlmCallError("timed_out", None)`.

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
    # Starts before the destination policy runs, so its DNS round-trip counts
    # against the same budget as the request it's guarding.
    deadline = time.monotonic() + timeout

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
    # The body is streamed rather than read in one shot so the deadline can be
    # enforced while it arrives -- see `_read_body_within_deadline`.
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(_CONNECT_TIMEOUT_S, timeout)),
            trust_env=False,
        ) as client:
            with client.stream(
                "POST",
                url,
                headers=headers,
                json=body,
                extensions=extensions,
            ) as response:
                status = response.status_code
                if not response.is_success:
                    logger.warning(
                        "LLM call failed for provider %s: http_%s",
                        credential.provider, status,
                    )
                    raise LlmCallError(f"http_{status}", status)
                raw = _read_body_within_deadline(response, deadline, credential.provider)
    except (httpx.HTTPError, httpx.StreamError) as exc:
        logger.warning(
            "LLM call failed for provider %s: %s",
            credential.provider, type(exc).__name__,
        )
        raise LlmCallError("connection_failed", None) from exc

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "LLM call returned unparseable JSON for provider %s: %s",
            credential.provider, type(exc).__name__,
        )
        raise LlmCallError("invalid_response", status) from exc

    # Explicit shape checks -- never let an uncaught IndexError/KeyError/
    # TypeError leak out of a malformed but "successful" reply.
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        _raise_invalid_response(credential.provider, status)

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        _raise_invalid_response(credential.provider, status)

    content = message.get("content")
    if not isinstance(content, str):
        _raise_invalid_response(credential.provider, status)

    return LlmCallResult(content=content, usage=_parse_usage(payload))
