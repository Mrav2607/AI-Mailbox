from __future__ import annotations

import json
from typing import Any

from app.core.config import settings
from app.core.logging import logger

LABELS = (
    "needs_reply",
    "action_required",
    "follow_up",
    "meeting",
    "personal",
    "work_project",
    "financial",
    "orders_shipping",
    "promotions",
    "updates_notifications",
    "security_account",
    "spam_junk",
    "other",
)


def _heuristic_classify(text: str) -> tuple[str, float, str, str]:
    lowered = (text or "").lower()
    if not lowered:
        return ("other", 0.1, "empty message", "heuristic-v1")

    if any(token in lowered for token in ["can you", "could you", "please", "?", "let me know"]):
        return ("needs_reply", 0.7, "contains reply request cues", "heuristic-v1")

    if any(token in lowered for token in ["order", "shipped", "shipping", "tracking", "delivery", "delivered"]):
        return ("orders_shipping", 0.65, "order/shipping keywords", "heuristic-v1")

    if any(token in lowered for token in ["invoice", "receipt", "payment", "billing", "due", "refund"]):
        return ("financial", 0.65, "financial keywords", "heuristic-v1")

    return ("other", 0.5, "no matching heuristics", "heuristic-v1")


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


def classify(text: str) -> tuple[str, float, str, str]:
    """
    LLM-backed classifier with heuristic fallback.
    Returns (label, confidence, rationale, model_version).
    """
    if not settings.gemini_api_key:
        return _heuristic_classify(text)

    prompt = (
        "Classify the email into one label from this list: "
        "needs_reply, action_required, follow_up, meeting, personal, work_project, "
        "financial, orders_shipping, promotions, updates_notifications, "
        "security_account, spam_junk, other. "
        "Return JSON only with keys: label, confidence (0-1), rationale. "
        "Be strict: needs_reply only if a reply is actually expected."
    )
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
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
