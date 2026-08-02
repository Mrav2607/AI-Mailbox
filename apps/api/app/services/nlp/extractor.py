from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone

import httpx

from app.core.logging import logger
from app.services.nlp.providers import DestinationRejected, LlmCredential, pin_custom_destination

# Labels the classifier can assign that make a message eligible for
# second-stage extraction (needs a reply, or a concrete off-email task).
ACTION_LABELS = ("needs_reply", "action_required")
ACTION_KINDS = ("reply", "payment", "signature", "form", "rsvp", "deadline", "other")

_TITLE_MAX_LEN = 80

# Fixture text for the /test route -- the same for every provider, so a
# credential's own claimed extraction quality never affects the yes/no of
# whether it's wired up correctly.
_TEST_CREDENTIAL_MESSAGE = "Reply to Bob's question by Friday"


@dataclass(frozen=True)
class ExtractedAction:
    """
    A concrete obligation pulled out of one message by the extraction call.

    `due_precision` is `None` iff `due_at` is `None`; "date" means the model
    only had a calendar date to go on (resolved to 23:59:59 UTC of that day),
    "datetime" means an actual time of day was stated or implied.
    """

    kind: str
    title: str
    due_at: datetime | None
    due_precision: str | None
    due_raw: str | None
    amount: float | None
    currency: str | None
    confidence: float
    model_version: str


class NoAction:
    """
    Sentinel result: the model looked at the message and affirmatively found
    no concrete task -- terminal, must not be retried. Distinct from `None`,
    which means the attempt itself failed (call/parse error) and is still
    retryable.
    """


class ExtractionCallError(Exception):
    """
    The single internal failure carrier for `_call_llm`: every failure mode
    -- destination policy, connection, non-2xx, or a malformed response
    shape -- raises this instead of letting an unrelated exception type
    escape. `category` is one of connection_failed | http_<status> |
    invalid_response | blocked_by_policy; `status` is the HTTP status code
    when one exists, else `None`. `extract_action` maps this to the
    unchanged public `None` contract; `test_credential` maps it to the
    /test route's category set directly -- neither derives a category from
    a lossy `None`.
    """

    def __init__(self, category: str, status: int | None) -> None:
        self.category = category
        self.status = status
        super().__init__(category)


def _build_message_text(
    subject: str | None, sender: str | None, snippet: str | None, body_text: str | None
) -> str:
    """Assemble the email text handed to the model alongside the prompt."""
    return "\n".join(
        [
            f"From: {sender or '(unknown)'}",
            f"Subject: {subject or '(no subject)'}",
            "",
            snippet or "",
            body_text or "",
        ]
    ).strip()


def _build_prompt(received_at: datetime | None) -> str:
    # received_at anchors relative deadlines ("by Friday", "end of week") --
    # without it the model would have to guess "today", and guessing a date
    # it can't see is exactly the invented-deadline failure mode this
    # feature exists to avoid.
    received_at_str = (
        received_at.astimezone(timezone.utc).isoformat()
        if received_at is not None
        else "unknown"
    )
    return (
        "You read one email already flagged as needing a reply or a "
        "concrete action from the RECIPIENT, and extract the single "
        "underlying obligation as structured JSON.\n\n"
        f"The message's received_at (ISO 8601 UTC) is: {received_at_str}. "
        "Resolve any relative deadline ('by Friday', 'end of week', 'in 3 "
        "days') against this timestamp -- never against today's date.\n\n"
        "Rules:\n"
        "- has_action: false if there is actually no concrete task -- a "
        "marketing CTA, an optional/automated 'confirm'/'renew'/'verify', "
        "or informational text with nothing for the recipient to DO. Same "
        "boundary discipline as classification: when unsure, false.\n"
        "- kind: one of reply, payment, signature, form, rsvp, deadline, "
        "other -- pick the closest match.\n"
        "- title: imperative and concrete (e.g. 'Pay invoice #429'), at "
        f"most {_TITLE_MAX_LEN} characters.\n"
        "- due_at: the resolved deadline as an ISO 8601 UTC timestamp, or "
        "null if no deadline is stated or clearly implied. NEVER invent a "
        "date that isn't in the text.\n"
        "- due_is_date_only: true if only a calendar date was given with "
        "no time of day -- in that case set due_at to 23:59:59 UTC of that "
        "date.\n"
        "- due_raw: the deadline phrase as written in the email (e.g. 'by "
        "end of week'), or null.\n"
        "- amount / currency: a stated monetary amount and its currency "
        "code, or null for both. NEVER invent an amount.\n"
        "- confidence: 0-1, how confident you are in this extraction.\n\n"
        "Return JSON only with keys: has_action, kind, title, due_at, "
        "due_is_date_only, due_raw, amount, currency, confidence."
    )


def _parse_due_at(due_at_raw: object, due_is_date_only: object) -> tuple[datetime | None, str | None]:
    if due_at_raw is None:
        return None, None
    if not isinstance(due_at_raw, str):
        raise ValueError("Invalid due_at")
    # A model reply that includes due_at but omits due_is_date_only is a
    # deterministic shape the retry loop can't fix, so treat missing/None as
    # False rather than burning all attempts on it. A PRESENT non-boolean
    # value is still a malformed response and still raises.
    if due_is_date_only is None:
        due_is_date_only = False
    elif not isinstance(due_is_date_only, bool):
        raise ValueError("Invalid due_is_date_only")

    try:
        parsed = datetime.fromisoformat(due_at_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("due_at is not valid ISO 8601") from exc

    if due_is_date_only:
        # Pin the time ourselves rather than trust the model zeroed it --
        # due_precision="date" downstream is a promise about the TIME
        # component, not just the calendar date.
        return (
            datetime.combine(parsed.date(), dtime(23, 59, 59), tzinfo=timezone.utc),
            "date",
        )

    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return parsed, "datetime"


def _parse_extraction(content: str, *, model_version: str) -> ExtractedAction | NoAction:
    """
    Strictly validate the model's JSON reply.

    `model_version` identifies which credential produced this extraction
    (`f"{provider}:{model}"`) -- BYOK means every row can come from a
    different provider/model, so stamping a single global default here
    would misattribute every extraction that isn't the operator's own.

    Raises `ValueError` or `json.JSONDecodeError` on anything invalid;
    callers treat both as an unusable response.
    """
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("Response is not a JSON object")

    has_action = payload.get("has_action")
    if not isinstance(has_action, bool):
        raise ValueError("Invalid has_action")
    if not has_action:
        return NoAction()

    kind = payload.get("kind")
    if kind not in ACTION_KINDS:
        raise ValueError("Invalid kind")

    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Invalid title")
    title = title.strip()[:_TITLE_MAX_LEN]

    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("Invalid confidence")
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        raise ValueError("Confidence out of range")

    due_raw = payload.get("due_raw")
    if due_raw is not None and not isinstance(due_raw, str):
        raise ValueError("Invalid due_raw")

    amount = payload.get("amount")
    if amount is not None and (not isinstance(amount, (int, float)) or isinstance(amount, bool)):
        raise ValueError("Invalid amount")
    amount = float(amount) if amount is not None else None

    currency = payload.get("currency")
    if currency is not None and not isinstance(currency, str):
        raise ValueError("Invalid currency")

    due_at, due_precision = _parse_due_at(payload.get("due_at"), payload.get("due_is_date_only"))

    return ExtractedAction(
        kind=kind,
        title=title,
        due_at=due_at,
        due_precision=due_precision,
        due_raw=due_raw,
        amount=amount,
        currency=currency,
        confidence=confidence,
        model_version=model_version,
    )


def _raise_invalid_response(provider: str, status: int) -> None:
    logger.warning("Action extraction returned a malformed response shape for provider %s", provider)
    raise ExtractionCallError("invalid_response", status)


def _call_llm(credential: LlmCredential, prompt: str, message_text: str) -> str:
    """
    Perform the OpenAI-compatible chat-completions call and return the raw
    `choices[0].message.content` string. Raises `ExtractionCallError` on
    every failure -- the single internal failure carrier.

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
            raise ExtractionCallError("blocked_by_policy", None) from exc

    body = {
        "model": credential.model,
        "messages": [
            {"role": "user", "content": f"{prompt}\n\nEmail:\n{message_text[:6000]}"}
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 512,
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
                    "Action extraction call failed for provider %s: http_%s",
                    credential.provider, status,
                )
                raise ExtractionCallError(f"http_{status}", status)
    except httpx.HTTPError as exc:
        logger.warning(
            "Action extraction call failed for provider %s: %s",
            credential.provider, type(exc).__name__,
        )
        raise ExtractionCallError("connection_failed", None) from exc

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        logger.warning(
            "Action extraction returned unparseable JSON for provider %s: %s",
            credential.provider, type(exc).__name__,
        )
        raise ExtractionCallError("invalid_response", response.status_code) from exc

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


def extract_action(
    *,
    subject: str | None,
    sender: str | None,
    snippet: str | None,
    body_text: str | None,
    received_at: datetime | None,
    credential: LlmCredential,
) -> ExtractedAction | NoAction | None:
    """
    Run the second-stage extraction call for one already-classified message,
    against the caller's resolved `credential` (own key, or the server
    fallback -- see `providers.resolve_extraction_credential`).

    Never raises. Returns `ExtractedAction` on success, `NoAction` when the
    model affirmatively found no concrete task (terminal), and `None` for
    any actual attempt failure -- call failure, destination policy
    rejection, or an unparseable/invalid response -- each logged as a
    warning. The three-way result contract is unchanged from the previous
    Gemini-SDK implementation.

    This does not check `settings.action_extraction_enabled` itself --
    callers only reach here after `extraction_run`'s preflight, which owns
    that gate (and the credential resolution that produces `credential`).
    """
    message_text = _build_message_text(subject, sender, snippet, body_text)
    prompt = _build_prompt(received_at)

    try:
        content = _call_llm(credential, prompt, message_text)
    except ExtractionCallError as exc:
        logger.warning(
            "Action extraction failed for provider %s: %s", credential.provider, exc.category
        )
        return None

    try:
        return _parse_extraction(content, model_version=f"{credential.provider}:{credential.model}")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Action extraction returned an unusable response for provider %s: %s",
            credential.provider, type(exc).__name__,
        )
        return None


def test_credential(credential: LlmCredential) -> tuple[bool, str | None, int]:
    """
    The `/test` route's entry point: runs `_call_llm` + `_parse_extraction`
    against a fixed fixture message and reports `(ok, category, latency_ms)`.
    Maps `ExtractionCallError.category` and a parse failure
    (`invalid_response`) directly -- the route never derives a category
    from a lossy `None`.
    """
    prompt = _build_prompt(None)
    start = time.monotonic()
    try:
        content = _call_llm(credential, prompt, _TEST_CREDENTIAL_MESSAGE)
    except ExtractionCallError as exc:
        return False, exc.category, int((time.monotonic() - start) * 1000)

    try:
        _parse_extraction(content, model_version=f"{credential.provider}:{credential.model}")
    except (json.JSONDecodeError, ValueError):
        return False, "invalid_response", int((time.monotonic() - start) * 1000)

    return True, None, int((time.monotonic() - start) * 1000)
