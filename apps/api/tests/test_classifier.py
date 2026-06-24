"""Unit tests for the classifier: heuristic rules + backend dispatch/fallback
(local/gemini/heuristic), all offline -- no LLM or network required."""

import pytest

from app.services.nlp.classifier import LABELS, _heuristic_classify, classify


@pytest.mark.parametrize(
    "text, expected_label",
    [
        ("Can you review the doc by EOD?", "needs_reply"),
        ("Please let me know your thoughts", "needs_reply"),
        ("Invoice #1842 is due Friday", "action_required"),
        ("Please verify your email to continue", "action_required"),
        ("Order shipped: track your package", "fyi"),
        ("Your weekly product digest", "fyi"),
        ("Promo: 30% off this weekend only", "promotional"),
        ("Security alert: new login detected", "security_alert"),
        ("You won a $1,000 gift card!!!", "spam"),
        ("", "fyi"),
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


def test_classify_gemini_backend_falls_back_to_heuristic_without_api_key(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "gemini")
    monkeypatch.setattr(classifier.settings, "gemini_api_key", None)
    label, confidence, rationale, model_version = classify("Can you review this?")
    assert label == "needs_reply"
    assert model_version == "heuristic-v1"


def test_classify_heuristic_backend(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "heuristic")
    label, confidence, rationale, model_version = classify("Security alert: new login detected")
    assert label == "security_alert"
    assert model_version == "heuristic-v1"


def test_classify_local_backend_falls_back_when_model_missing(monkeypatch, tmp_path):
    # With no model on disk, the local backend must degrade to the gemini/
    # heuristic path rather than raise.
    from app.services.nlp import classifier, local_model

    local_model.reset()
    monkeypatch.setattr(classifier.settings, "classifier_backend", "local")
    monkeypatch.setattr(classifier.settings, "classifier_model_path", str(tmp_path / "no-model-here"))
    monkeypatch.setattr(classifier.settings, "gemini_api_key", None)

    label, confidence, rationale, model_version = classify("Can you review this?")
    assert label in LABELS
    assert model_version == "heuristic-v1"
    local_model.reset()
