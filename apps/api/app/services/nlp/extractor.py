from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone

from app.core.logging import logger
from app.services.nlp.llm_client import LlmCallError, LlmCallResult, LlmUsage, call_chat_completion
from app.services.nlp.providers import LlmCredential

# Kept as an alias so existing importers of the old name (tests included)
# don't break -- the wire call and its error type moved to llm_client, but
# this module still owns extraction's category-to-result mapping.
ExtractionCallError = LlmCallError

# The extraction call's own token budget -- classification (a different
# caller of call_chat_completion) sets its own.
_MAX_TOKENS = 512

# The extraction call truncates the message body before sending it -- kept
# here since it's specific to how extraction builds its user content, not a
# wire-level concern.
_MESSAGE_TEXT_MAX_LEN = 6000

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


@dataclass(frozen=True)
class ExtractionAttempt:
    """
    Everything one `_extract_attempt` call produced, including whether it
    reached the provider at all.

    `provider_call_succeeded` and `usage is not None` are NOT the same
    question. `provider_call_succeeded` means the wire call came back --
    the provider was reached and, for a BYOK credential, charged for it --
    regardless of what came back afterward. `usage` is just whatever token
    telemetry rode along on that response, which some providers omit even on
    a perfectly good call. A response that comes back fine but fails to
    parse still has `provider_call_succeeded=True` and possibly `usage=None`:
    the user already paid for that call, so it must still be counted.

    `llm_attempted` / `fallback_used` / `failure_category` are the failure-
    visibility fields (plan: `docs/plans/2026-08-14-llm-failure-visibility-
    plan.md`), the same contract as `ClassificationAttempt`. Extraction has
    no encoder/heuristic fallback -- `_extract_attempt` always issues exactly
    one provider request -- so `llm_attempted` is always True and
    `fallback_used` is always False here; only `failure_category` varies
    (`LlmCallError.category`, or `"invalid_response"` for a malformed body
    that isn't an `LlmCallError` at all).
    """

    result: ExtractedAction | NoAction | None
    provider_call_succeeded: bool
    usage: LlmUsage | None
    llm_attempted: bool = False
    fallback_used: bool = False
    failure_category: str | None = None


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


def _call_llm(credential: LlmCredential, prompt: str, message_text: str) -> LlmCallResult:
    """
    Thin wrapper around the shared wire call (`llm_client.call_chat_completion`)
    that adds extraction's own framing -- the "Email:" label and the 6000-char
    truncation -- around the message text. Raises `ExtractionCallError`
    (== `LlmCallError`) on every failure.

    Returns the full `LlmCallResult`, not just `.content` -- `_extract_attempt`
    needs the `usage` alongside it. `test_credential` only reads `.content`
    and deliberately drops the rest (see its docstring).
    """
    user_content = f"Email:\n{message_text[:_MESSAGE_TEXT_MAX_LEN]}"
    return call_chat_completion(
        credential, prompt=prompt, user_content=user_content, max_tokens=_MAX_TOKENS
    )


def _extract_attempt(
    *,
    subject: str | None,
    sender: str | None,
    snippet: str | None,
    body_text: str | None,
    received_at: datetime | None,
    credential: LlmCredential,
) -> ExtractionAttempt:
    """
    The one real implementation behind `extract_action` and
    `extract_action_with_usage` -- both are thin wrappers around this so
    usage-tracking can never drift out of sync with the plain three-way
    result again.

    `provider_call_succeeded` flips `True` the moment `_call_llm` returns,
    before any parsing happens -- a BYOK credential is charged for the call
    whether or not its body turns out to be usable, so a parse failure must
    still count. Only `ExtractionCallError` (the call never came back at
    all) leaves it `False`.
    """
    message_text = _build_message_text(subject, sender, snippet, body_text)
    prompt = _build_prompt(received_at)

    try:
        call_result = _call_llm(credential, prompt, message_text)
    except ExtractionCallError as exc:
        logger.warning(
            "Action extraction failed for provider %s: %s", credential.provider, exc.category
        )
        return ExtractionAttempt(
            result=None,
            provider_call_succeeded=False,
            usage=None,
            llm_attempted=True,
            fallback_used=False,
            failure_category=exc.category,
        )

    try:
        result = _parse_extraction(
            call_result.content, model_version=f"{credential.provider}:{credential.model}"
        )
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Action extraction returned an unusable response for provider %s: %s",
            credential.provider, type(exc).__name__,
        )
        return ExtractionAttempt(
            result=None,
            provider_call_succeeded=True,
            usage=call_result.usage,
            llm_attempted=True,
            fallback_used=False,
            failure_category="invalid_response",
        )

    return ExtractionAttempt(
        result=result,
        provider_call_succeeded=True,
        usage=call_result.usage,
        llm_attempted=True,
        fallback_used=False,
        failure_category=None,
    )


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

    Thin wrapper over `_extract_attempt` -- drops the usage/call-succeeded
    bookkeeping for callers that don't need it. Production callers that bill
    a user's key should use `extract_action_with_usage` instead so that
    spend actually gets recorded.
    """
    return _extract_attempt(
        subject=subject,
        sender=sender,
        snippet=snippet,
        body_text=body_text,
        received_at=received_at,
        credential=credential,
    ).result


def extract_action_with_usage(
    *,
    subject: str | None,
    sender: str | None,
    snippet: str | None,
    body_text: str | None,
    received_at: datetime | None,
    credential: LlmCredential,
) -> ExtractionAttempt:
    """
    Same call and args as `extract_action`, but returns the full
    `ExtractionAttempt` -- including `provider_call_succeeded` and `usage` --
    for callers that need to record background usage (Wave 2b wires the
    actual call sites; this wave only lands the contract).
    """
    return _extract_attempt(
        subject=subject,
        sender=sender,
        snippet=snippet,
        body_text=body_text,
        received_at=received_at,
        credential=credential,
    )


def test_credential(credential: LlmCredential) -> tuple[bool, str | None, int]:
    """
    The `/test` route's entry point: runs `_call_llm` + `_parse_extraction`
    against a fixed fixture message and reports `(ok, category, latency_ms)`.
    Maps `ExtractionCallError.category` and a parse failure
    (`invalid_response`) directly -- the route never derives a category
    from a lossy `None`.

    Deliberately does not record or return usage, even though `_call_llm`
    carries it now -- this is an explicit v1 non-goal (plan §1): the route
    is already rate-limited, user-initiated, and shows its result on screen
    immediately, unlike background classification/extraction. Don't wire
    usage in here later just because it's sitting right there.
    """
    prompt = _build_prompt(None)
    start = time.monotonic()
    try:
        call_result = _call_llm(credential, prompt, _TEST_CREDENTIAL_MESSAGE)
    except ExtractionCallError as exc:
        return False, exc.category, int((time.monotonic() - start) * 1000)

    try:
        _parse_extraction(call_result.content, model_version=f"{credential.provider}:{credential.model}")
    except (json.JSONDecodeError, ValueError):
        return False, "invalid_response", int((time.monotonic() - start) * 1000)

    return True, None, int((time.monotonic() - start) * 1000)
