from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.services.nlp.llm_client import LlmCallError, LlmUsage, call_chat_completion
from app.services.nlp.providers import ClassificationRouting, LlmCredential

LABELS = (
    "needs_reply",
    "action_required",
    "fyi",
    "promotional",
    "security_alert",
    "spam",
)


@dataclass(frozen=True)
class ClassificationAttempt:
    """
    One classification call's full outcome -- `classify()` keeps returning
    just `verdict` (today's unchanged contract); `classify_with_usage()`
    returns the whole thing so a paid call site can record what it spent.

    `provider_call_succeeded` and `usage` are deliberately two separate
    fields, not one derived from the other. `usage is None` is ambiguous on
    its own: it's also true of every heuristic/local verdict that never
    touched a provider, AND of a successful call whose provider simply
    didn't report token counts (`usage` is optional in the OpenAI spec).
    `provider_call_succeeded` answers the narrower question the usage
    recorder actually needs -- did a request reach a provider and come back
    at all -- and it's `True` even when the response body fails to parse and
    we fall back to the heuristic. That case matters most: the provider
    already answered (and, on BYOK, already billed the user) by the time
    parsing happens, so undercounting it would recreate the exact
    quota-blindness this feature exists to fix.
    """

    verdict: tuple[str, float, str, str]
    provider_call_succeeded: bool
    usage: LlmUsage | None


def _heuristic_attempt(text: str) -> ClassificationAttempt:
    """The heuristic never touches a provider, on any path that reaches it --
    shared by the `backend="heuristic"` route and every LLM-path fallback
    that decides not to call anyone (routing `off`, a missing credential)."""
    return ClassificationAttempt(
        verdict=_heuristic_classify(text), provider_call_succeeded=False, usage=None
    )


def build_classification_text(
    subject: str | None, snippet: str | None, body_text: str | None
) -> str:
    """
    Assemble the model input from a message's parts.

    The classifier was trained on subject + snippet + body_text. Every serving
    path MUST build the text the same way, or the model sees a different input
    distribution than it was trained on. This is the single source of truth --
    do not hand-assemble the text anywhere else.
    """
    return " ".join([subject or "", snippet or "", body_text or ""]).strip()


def _heuristic_classify(text: str) -> tuple[str, float, str, str]:
    lowered = (text or "").lower()
    if not lowered:
        return ("fyi", 0.1, "empty message", "heuristic-v1")

    if any(token in lowered for token in [
        "security alert", "new login", "new sign-in", "suspicious",
        "unauthorized", "verification code", "2fa", "unusual sign-in",
        "unusual activity", "password was reset",
    ]):
        return ("security_alert", 0.7, "security/account keywords", "heuristic-v1")

    if any(token in lowered for token in [
        "you won", "winner", "claim your prize", "lottery", "free money",
        "gift card", "congratulations you",
    ]):
        return ("spam", 0.7, "spam/scam keywords", "heuristic-v1")

    # Action keywords are checked before reply cues so an explicit task
    # ("verify your email") isn't swallowed by a generic "please"/"?".
    if any(token in lowered for token in [
        "invoice", "due", "past due", "rsvp", "verify your", "confirm your",
        "action required", "expires", "renew", "complete your", "sign here",
    ]):
        return ("action_required", 0.6, "task/action keywords", "heuristic-v1")

    if any(token in lowered for token in ["can you", "could you", "please", "?", "let me know"]):
        return ("needs_reply", 0.65, "reply request cues", "heuristic-v1")

    if any(token in lowered for token in [
        "% off", "sale", "deal", "discount", "promo", "coupon",
        "limited time", "shop now", "unsubscribe", "flash sale",
    ]):
        return ("promotional", 0.6, "marketing keywords", "heuristic-v1")

    return ("fyi", 0.4, "no actionable cues", "heuristic-v1")


def _parse_llm_response(content: str) -> tuple[str, float, str]:
    payload = json.loads(content)
    label = payload.get("label")
    confidence = payload.get("confidence")  
    rationale = payload.get("rationale", "")
    if label not in LABELS:
        raise ValueError("Invalid label")
    if not isinstance(confidence, (int, float)):
        raise ValueError("Invalid confidence")
    return label, float(confidence), str(rationale)


def classify(
    text: str,
    backend: str | None = None,
    routing: ClassificationRouting | None = None,
) -> tuple[str, float, str, str]:
    """
    Classify an email into the 6-label taxonomy.
    Returns (label, confidence, rationale, model_version).

    Routed by `backend` (falling back to settings.classifier_backend when not
    given, so callers can override the global default per request):
      - "local":     fine-tuned encoder in models/, falling back to the LLM /
                     heuristic path if the model or its deps are unavailable.
      - "llm":       LLM with heuristic fallback. Which provider actually gets
                     called comes from `routing`, not from this name -- it was
                     called "gemini" before BYOK, which read as though it
                     pinned the provider. "gemini" still works as an alias.
      - "heuristic": keyword rules only.
      - "auto":      try local, then LLM, then heuristic.

    `routing` (see `providers.ClassificationRouting`) decides WHO PAYS if and
    only if the LLM path is actually reached -- it never overrides `backend`
    and never bypasses a locally-available encoder, since local inference is
    free for everyone and spending anyone's key instead would be strictly
    worse. `None` and `mode="server"` are byte-identical to the pre-BYOK
    behavior (the operator's key or heuristic); `mode="user"` spends the
    caller's own credential via the shared OpenAI-compatible wire client;
    `mode="off"` classifies heuristically without calling any LLM at all, so
    neither key is spent.

    This is the compatibility wrapper -- see `_classify_attempt` for the
    actual implementation, which `classify_with_usage()` also wraps. Any
    caller that can reach an LLM path (i.e. anywhere `routing` might resolve
    to `"user"`) MUST use `classify_with_usage()` instead, or its usage never
    gets recorded.
    """
    return _classify_attempt(text, backend, routing).verdict


def classify_with_usage(
    text: str,
    backend: str | None = None,
    routing: ClassificationRouting | None = None,
) -> ClassificationAttempt:
    """Same inputs and behavior as `classify()`, but returns the full
    `ClassificationAttempt` so a paid call site can record what it spent.
    Every production call site that can reach an LLM must call this, not
    the bare `classify()`."""
    return _classify_attempt(text, backend, routing)


def _classify_attempt(
    text: str,
    backend: str | None = None,
    routing: ClassificationRouting | None = None,
) -> ClassificationAttempt:
    """
    The one real implementation behind `classify()` and `classify_with_usage()`
    -- see those two docstrings for the routing/backend contract itself. Kept
    as a single implementation with two thin wrappers on purpose: two
    separate implementations would drift, and the drift most likely to
    happen is one of them silently forgetting to record usage.
    """
    backend = (backend or settings.classifier_backend or "auto").lower()

    if backend == "heuristic":
        return _heuristic_attempt(text)

    if backend in ("local", "auto"):
        from app.services.nlp.local_model import try_predict

        result = try_predict(text)
        if result is not None:
            return ClassificationAttempt(
                verdict=result, provider_call_succeeded=False, usage=None
            )
        # local unavailable -> fall through to the LLM / heuristic path

    return _classify_llm(text, routing)


@lru_cache(maxsize=1)
def _genai_client():
    """Build the Gemini client once per process (settings don't change at
    runtime, so there's nothing to key on). The explicit timeout keeps a hung
    call from pinning a threadpool thread forever -- the SDK measures it in
    milliseconds."""
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(timeout=30_000),
    )


# Shared by both LLM paths (native google-genai on the server key, and the
# OpenAI-compat call on a user's BYOK credential) -- the BYOK path's fallback
# test asserts the SAME strict parse a bad reply hits either way, so the two
# paths must be judging the model against identical wording.
_CLASSIFICATION_PROMPT = (
    "You classify an email into exactly ONE label, from the RECIPIENT's point "
        "of view. Decide what the recipient must actually DO.\n\n"
        "Labels:\n"
        "- needs_reply: the recipient is personally expected to WRITE BACK. A real "
        "person asks them a question, requests info, or awaits their response.\n"
        "- action_required: the recipient must personally complete a concrete "
        "off-email task with a real consequence or deadline -- pay an invoice, sign "
        "a document, submit a form, RSVP to a real invitation, reset a password they "
        "must change. NOT a reply, and NOT optional.\n"
        "- fyi: informational / automated / transactional mail to read for awareness. "
        "Receipts, order & shipping updates, notifications, statements, newsletters "
        "you subscribed to, calendar notices, app/system alerts. This is the DEFAULT "
        "when no genuine personal task or reply is required.\n"
        "- promotional: marketing, sales, offers, deals, or bulk mail trying to get "
        "you to buy or click. Has a commercial/advertising intent.\n"
        "- security_alert: account or login security -- verification codes, new "
        "sign-ins, suspicious activity, password/2FA notices.\n"
        "- spam: junk, scams, or phishing.\n\n"
        "CRITICAL boundary rules (this is where mistakes happen):\n"
        "1. Marketing CTAs are NOT action_required. 'Shop now', 'click here', "
        "'limited time', 'upgrade today', auto-renewal notices -> promotional or fyi.\n"
        "2. A soft/optional/automated 'confirm', 'renew', 'click', 'verify' is NOT "
        "action_required. Only use action_required when the recipient genuinely has "
        "to do the task or face a consequence.\n"
        "3. When unsure between action_required and fyi, choose fyi. Most automated "
        "and bulk email is fyi.\n"
        "4. needs_reply requires a real human awaiting YOUR written response, not an "
        "automated 'do not reply' message.\n\n"
        "Examples:\n"
        "- 'Your Amazon order has shipped, arriving Tuesday' -> fyi\n"
        "- 'Your monthly statement is ready to view' -> fyi\n"
        "- 'Invoice #429 is due Friday, pay to avoid late fees' -> action_required\n"
        "- 'Please sign the attached contract by EOD' -> action_required\n"
        "- 'Hey, can you send me the report when you get a chance?' -> needs_reply\n"
        "- '50% off this weekend only -- shop now!' -> promotional\n"
        "- 'New sign-in to your account from a new device' -> security_alert\n\n"
        "Return JSON only with keys: label, confidence (0-1), rationale."
    )

# The BYOK classification call's own token budget -- a label/confidence/
# rationale reply is far smaller than extraction's structured JSON, so this
# is deliberately tighter than extractor.py's 512.
_CLASSIFICATION_MAX_TOKENS = 200

# Classification runs once per ingested message, so a hung endpoint at
# extraction's 30s default could stall a whole Gmail sweep for hours before
# degrading to the heuristic. A tiny JSON label doesn't need that headroom --
# extractor.py stays at 30s since it only runs over a bounded subset of
# messages and produces longer output. This is a TOTAL wall-clock budget for
# the call, so it's a real per-message ceiling: worst case a full sweep costs
# this many seconds times the message count, and never more.
_CLASSIFICATION_TIMEOUT_S = 10.0


def _classify_llm(
    text: str, routing: ClassificationRouting | None = None
) -> ClassificationAttempt:
    """
    Dispatch to the LLM path that pays for this call, per `routing`
    (see `providers.ClassificationRouting` and `classify`'s docstring for the
    full precedence rules). `None`/`mode="server"` is the unchanged native
    google-genai path; `mode="user"` is the OpenAI-compatible BYOK path;
    `mode="off"` never calls either -- straight to the heuristic classifier,
    so the operator is never billed for a message the user opted out of.
    """
    if routing is not None and routing.mode == "off":
        return _heuristic_attempt(text)

    if routing is not None and routing.mode == "user":
        if routing.credential is None:
            # This should never happen -- "user" mode is supposed to always carry
            # a credential -- but `assert` gets stripped under `-O`, and a plain
            # AttributeError from call_chat_completion() isn't an LlmCallError, so
            # it would escape classify() and blow up the whole ingest run instead
            # of degrading gracefully. Heuristic, not the server path: a broken
            # invariant must never silently bill the operator for a user who
            # opted in with their own key.
            logger.warning("ClassificationRouting mode='user' had no credential")
            return _heuristic_attempt(text)
        return _classify_llm_user(text, routing.credential)

    return _classify_llm_server(text)


def _classify_llm_server(text: str) -> ClassificationAttempt:
    """LLM-backed classifier with heuristic fallback, on the operator's
    Gemini key. Unchanged from before BYOK classification existed."""
    if not settings.gemini_api_key:
        return _heuristic_attempt(text)

    # Each step below catches only what it can actually fail with. The old blanket
    # `except Exception` also swallowed our own bugs -- a typo in here came back as
    # a confident heuristic answer instead of a 500, which is exactly how a broken
    # classifier hides in plain sight.
    def fallback(reason: str, exc: Exception) -> ClassificationAttempt:
        # Every path through here never reached (or never finished talking
        # to) Gemini, so `provider_call_succeeded` stays False.
        logger.warning("Gemini classify %s, falling back to heuristic: %s", reason, exc)
        label, confidence, rationale, _ = _heuristic_classify(text)
        return ClassificationAttempt(
            verdict=(label, confidence, rationale, "heuristic-fallback"),
            provider_call_succeeded=False,
            usage=None,
        )

    try:
        from google.genai import errors as genai_errors
        from google.genai import types
    except ImportError as exc:
        return fallback("SDK is unavailable", exc)

    try:
        response = _genai_client().models.generate_content(
            model=settings.gemini_model,
            contents=f"{_CLASSIFICATION_PROMPT}\n\nEmail:\n{text[:6000]}",
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
    except (genai_errors.APIError, httpx.HTTPError, TimeoutError) as exc:
        # Gemini refused, rate-limited us, or never answered -- no call
        # completed.
        return fallback("call failed", exc)

    # A response came back: the call reached Gemini and completed, whatever
    # its content turns out to be. `provider_call_succeeded` is True from
    # here on even if the parse below fails.
    try:
        label, confidence, rationale = _parse_llm_response(response.text or "")
    except (json.JSONDecodeError, ValueError) as exc:
        # A reply we can't map onto our taxonomy is the model's problem, not
        # ours -- but the call still happened, so it still counts.
        logger.warning("Gemini classify returned an unusable answer, falling back to heuristic: %s", exc)
        label, confidence, rationale, _ = _heuristic_classify(text)
        return ClassificationAttempt(
            verdict=(label, confidence, rationale, "heuristic-fallback"),
            provider_call_succeeded=True,
            usage=None,
        )

    return ClassificationAttempt(
        verdict=(label, confidence, rationale, settings.gemini_model),
        provider_call_succeeded=True,
        # Operator-paid usage is an explicit v1 non-goal (plan §1), and Wave
        # 2b's recorder filters this path out via `routing.mode` anyway --
        # parsing `response.usage_metadata` here would be dead code implying
        # a capability we don't actually expose. Don't "fix" this.
        usage=None,
    )


def _classify_llm_user(text: str, credential: LlmCredential) -> ClassificationAttempt:
    """
    BYOK classification path: the same prompt and the same strict parse as
    the server path, wired through the shared OpenAI-compatible call instead
    of the native genai SDK. `model_version` is `f"{provider}:{model}"` --
    deliberately different attribution from the server path's bare model
    name, since a BYOK verdict can come from any provider/model the user
    picked, not the operator's fixed Gemini deployment.
    """
    try:
        result = call_chat_completion(
            credential,
            prompt=_CLASSIFICATION_PROMPT,
            user_content=f"Email:\n{text[:6000]}",
            max_tokens=_CLASSIFICATION_MAX_TOKENS,
            timeout=_CLASSIFICATION_TIMEOUT_S,
        )
    except LlmCallError as exc:
        logger.warning(
            "Classification call failed for provider %s: %s", credential.provider, exc.category
        )
        label, confidence, rationale, _ = _heuristic_classify(text)
        return ClassificationAttempt(
            verdict=(label, confidence, rationale, "heuristic-fallback"),
            provider_call_succeeded=False,
            usage=None,
        )

    # The call reached the provider and came back -- it already billed the
    # user's key by this point, so `provider_call_succeeded` is True and
    # `usage` rides along regardless of whether the content below parses.
    try:
        label, confidence, rationale = _parse_llm_response(result.content)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Classification returned an unusable response for provider %s: %s",
            credential.provider, type(exc).__name__,
        )
        label, confidence, rationale, _ = _heuristic_classify(text)
        return ClassificationAttempt(
            verdict=(label, confidence, rationale, "heuristic-fallback"),
            provider_call_succeeded=True,
            usage=result.usage,
        )

    return ClassificationAttempt(
        verdict=(label, confidence, rationale, f"{credential.provider}:{credential.model}"),
        provider_call_succeeded=True,
        usage=result.usage,
    )
