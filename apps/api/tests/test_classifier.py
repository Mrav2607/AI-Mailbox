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


def test_warmup_caches_failure_and_is_a_noop_after(monkeypatch):
    # A failed warmup must flag the model unavailable once and never re-attempt
    # the load -- not from warmup() again, not from try_predict.
    from app.services.nlp import local_model

    local_model.reset()
    load_attempts = []

    def failing_load():
        load_attempts.append(1)
        raise FileNotFoundError("no model on disk")

    monkeypatch.setattr(local_model, "_load", failing_load)

    local_model.warmup()
    assert local_model._unavailable is True
    assert local_model.try_predict("hello") is None
    local_model.warmup()  # cheap no-op now
    assert load_attempts == [1]
    local_model.reset()


def test_warmup_is_a_noop_when_already_loaded(monkeypatch):
    from app.services.nlp import local_model

    local_model.reset()
    monkeypatch.setattr(local_model, "_state", ("tok", "model", ["a"], "cpu", "v1"))
    monkeypatch.setattr(local_model, "_load", lambda: pytest.fail("must not reload"))
    local_model.warmup()
    assert local_model._unavailable is False


def test_try_predict_fast_path_skips_the_load_lock(monkeypatch):
    # Once _state is populated, try_predict must serve without touching the
    # load lock -- we plant a lock that blows up if anyone enters it.
    torch = pytest.importorskip("torch")
    from app.services.nlp import local_model

    local_model.reset()

    class Encoding(dict):
        def to(self, device):
            return self

    class FakeOutput:
        logits = torch.tensor([[0.1, 5.0, 0.2]])

    def fake_tokenizer(text, **kwargs):
        return Encoding(input_ids=torch.tensor([[1, 2]]))

    class ExplodingLock:
        def __enter__(self):
            raise AssertionError("fast path acquired the load lock")

        def __exit__(self, *args):
            return False

    state = (fake_tokenizer, lambda **enc: FakeOutput(), ["a", "b", "c"], "cpu", "test")
    monkeypatch.setattr(local_model, "_state", state)
    monkeypatch.setattr(local_model, "_lock", ExplodingLock())

    result = local_model.try_predict("hello")
    assert result is not None
    label, confidence, _rationale, model_version = result
    assert label == "b"
    assert 0.0 < confidence <= 1.0
    assert model_version == "local:test"


def test_try_predict_falls_back_when_all_infer_slots_are_busy(monkeypatch):
    # With every inference slot held, try_predict must give up after the
    # bounded wait and return None (LLM/heuristic fallback) instead of
    # queueing the request thread forever.
    pytest.importorskip("torch")
    from threading import Semaphore

    from app.services.nlp import local_model

    local_model.reset()
    monkeypatch.setattr(
        local_model, "_state", ("tok", "model", ["a"], "cpu", "test")
    )
    monkeypatch.setattr(local_model, "_infer_slots", Semaphore(0))
    monkeypatch.setattr(local_model, "_SLOT_TIMEOUT_S", 0.01)

    assert local_model.try_predict("hello") is None


def test_genai_client_is_cached_across_calls(monkeypatch):
    pytest.importorskip("google.genai")
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "gemini_api_key", "test-key")
    classifier._genai_client.cache_clear()
    try:
        first = classifier._genai_client()
        assert classifier._genai_client() is first
    finally:
        # Don't leave a client built from the test key cached for other tests.
        classifier._genai_client.cache_clear()
