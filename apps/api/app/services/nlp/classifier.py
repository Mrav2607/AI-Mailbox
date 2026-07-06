from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.core.logging import logger

LABELS = (
    "needs_reply",
    "action_required",
    "fyi",
    "promotional",
    "security_alert",
    "spam",
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


def classify(text: str, backend: str | None = None) -> tuple[str, float, str, str]:
    """
    Classify an email into the 6-label taxonomy.
    Returns (label, confidence, rationale, model_version).

    Routed by `backend` (falling back to settings.classifier_backend when not
    given, so callers can override the global default per request):
      - "local":     fine-tuned encoder in models/, falling back to the LLM /
                     heuristic path if the model or its deps are unavailable.
      - "gemini":    LLM with heuristic fallback (the original behavior).
      - "heuristic": keyword rules only.
      - "auto":      try local, then LLM, then heuristic.
    """
    backend = (backend or settings.classifier_backend or "auto").lower()

    if backend == "heuristic":
        return _heuristic_classify(text)

    if backend in ("local", "auto"):
        from app.services.nlp.local_model import try_predict

        result = try_predict(text)
        if result is not None:
            return result
        # local unavailable -> fall through to the LLM / heuristic path

    return _classify_llm(text)


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


def _classify_llm(text: str) -> tuple[str, float, str, str]:
    """LLM-backed classifier with heuristic fallback."""
    if not settings.gemini_api_key:
        return _heuristic_classify(text)

    prompt = (
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
    try:
        from google.genai import types

        response = _genai_client().models.generate_content(
            model=settings.gemini_model,
            contents=f"{prompt}\n\nEmail:\n{text[:6000]}",
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        content = response.text or ""
        label, confidence, rationale = _parse_llm_response(content)
        return (label, confidence, rationale, settings.gemini_model)
    except Exception as exc:
        logger.warning("Classify failed: %s", exc)
    label, confidence, rationale, _ = _heuristic_classify(text)
    return (label, confidence, rationale, "heuristic-fallback")
