"""Unit tests for the heuristic classifier (no LLM / network required)."""

import pytest

from app.services.nlp.classifier import LABELS, _heuristic_classify, classify


@pytest.mark.parametrize(
    "text, expected_label",
    [
        ("Can you review the doc by EOD?", "needs_reply"),
        ("Please let me know your thoughts", "needs_reply"),
        ("Order shipped: track your package", "orders_shipping"),
        ("Invoice #1842 is due", "financial"),
        ("Refund processed for your payment", "financial"),
        ("FYI: interesting article", "other"),
        ("", "other"),
    ],
)
def test_heuristic_labels(text, expected_label):
    label, confidence, rationale, model_version = _heuristic_classify(text)
    assert label == expected_label
    assert 0.0 <= confidence <= 1.0
    assert model_version == "heuristic-v1"


@pytest.mark.parametrize(
    "text",
    [
        "Can you help?",
        "Order shipped",
        "Invoice due",
        "random unmatched text",
        "",
    ],
)
def test_heuristic_only_returns_canonical_labels(text):
    # Regression guard: the heuristic must never emit a label outside LABELS.
    label, *_ = _heuristic_classify(text)
    assert label in LABELS


def test_classify_falls_back_to_heuristic_without_api_key(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "gemini_api_key", None)
    label, confidence, rationale, model_version = classify("Can you review this?")
    assert label == "needs_reply"
    assert model_version == "heuristic-v1"
