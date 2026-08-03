"""Unit tests for the classifier: heuristic rules + backend dispatch/fallback
(local/gemini/heuristic), all offline -- no LLM or network required."""

import json

import pytest

from app.services.nlp.classifier import LABELS, _heuristic_classify, classify
from app.services.nlp.llm_client import LlmCallError, LlmCallResult
from app.services.nlp.providers import ClassificationRouting, LlmCredential


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


# ---------------------------------------------------------------------------
# Classification routing: tri-state dispatch inside _classify_llm, and the
# frozen precedence matrix against CLASSIFIER_BACKEND / the local encoder.
# ---------------------------------------------------------------------------


def _fake_genai_response(label="fyi", confidence=0.8, rationale="test"):
    class _FakeResponse:
        text = json.dumps({"label": label, "confidence": confidence, "rationale": rationale})

    class _FakeModels:
        def generate_content(self, **kwargs):
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    return _FakeClient()


def _explode(*args, **kwargs):
    raise AssertionError("this call must never happen for this routing mode")


def test_classify_routing_none_and_server_are_byte_identical_no_key(monkeypatch):
    """With no gemini_api_key configured, routing=None and mode="server"
    must both fall back to heuristic identically -- BYOK is additive."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "gemini")
    monkeypatch.setattr(classifier.settings, "gemini_api_key", None)

    result_none = classify("Can you review this?", routing=None)
    result_server = classify(
        "Can you review this?", routing=ClassificationRouting(mode="server", credential=None)
    )
    assert result_none == result_server
    assert result_none[3] == "heuristic-v1"


def test_classify_routing_none_and_server_are_byte_identical_with_genai(monkeypatch):
    """Same as above but through a mocked successful genai call -- both
    routing=None and mode="server" must reach the native path and stamp the
    bare model name, unchanged from before BYOK classification existed."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "gemini")
    monkeypatch.setattr(classifier.settings, "gemini_api_key", "server-key")
    monkeypatch.setattr(classifier.settings, "gemini_model", "gemini-2.5-flash")
    monkeypatch.setattr(
        classifier, "_genai_client", lambda: _fake_genai_response(label="fyi")
    )

    for routing in (None, ClassificationRouting(mode="server", credential=None)):
        label, confidence, rationale, model_version = classify("hi there", routing=routing)
        assert label == "fyi"
        assert model_version == "gemini-2.5-flash"  # bare model name -- server attribution


def test_classify_routing_user_mode_calls_openai_compat_with_credential(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "gemini")
    monkeypatch.setattr(classifier, "_genai_client", _explode)  # must never be built for mode="user"

    credential = LlmCredential(
        provider="openai", base_url="https://api.openai.com/v1", api_key="user-key", model="gpt-4o-mini"
    )
    captured = {}

    def fake_call(cred, *, prompt, user_content, max_tokens, timeout):
        captured["credential"] = cred
        captured["timeout"] = timeout
        content = json.dumps({"label": "spam", "confidence": 0.9, "rationale": "scam"})
        return LlmCallResult(content=content, usage=None)

    monkeypatch.setattr(classifier, "call_chat_completion", fake_call)

    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify("You won a prize!", routing=routing)

    assert label == "spam"
    assert model_version == "openai:gpt-4o-mini"  # BYOK attribution -- provider:model
    assert captured["credential"] is credential
    # Classification uses its own tighter timeout, not extraction's 30s default.
    assert captured["timeout"] == classifier._CLASSIFICATION_TIMEOUT_S


def test_classify_routing_off_mode_never_builds_genai_client_or_calls_llm(monkeypatch):
    """The point of the seam assertions here: proving the operator was not
    billed, not merely checking the returned label."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "gemini")
    monkeypatch.setattr(classifier, "_genai_client", _explode)
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)

    routing = ClassificationRouting(mode="off", credential=None)
    label, confidence, rationale, model_version = classify("Can you help?", routing=routing)

    assert label == "needs_reply"
    assert model_version == "heuristic-v1"  # direct heuristic, not "-fallback"


def test_classify_routing_user_mode_with_no_credential_falls_back_to_heuristic(monkeypatch):
    """Guards the broken-invariant path: `assert` gets stripped under `-O`,
    so mode="user" with credential=None must be a real guard that returns
    the heuristic, not a crash or a silent fall-through to the server path
    (which would bill the operator for a user who opted in with their own
    key)."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "gemini")
    monkeypatch.setattr(classifier, "_genai_client", _explode)
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)

    routing = ClassificationRouting(mode="user", credential=None)
    label, confidence, rationale, model_version = classify(
        "Security alert: new login detected", routing=routing
    )
    assert label == "security_alert"
    assert model_version == "heuristic-v1"  # direct heuristic, not "-fallback"


def test_classify_routing_user_mode_falls_back_to_heuristic_on_call_error(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "gemini")
    credential = LlmCredential(
        provider="groq", base_url="https://api.groq.com/openai/v1", api_key="k", model="llama"
    )

    def failing_call(*args, **kwargs):
        raise LlmCallError("connection_failed", None)

    monkeypatch.setattr(classifier, "call_chat_completion", failing_call)
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify(
        "Security alert: new login detected", routing=routing
    )
    assert label == "security_alert"
    assert model_version == "heuristic-fallback"


def test_classify_routing_user_mode_falls_back_to_heuristic_on_unparseable_response(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "gemini")
    credential = LlmCredential(
        provider="mistral", base_url="https://api.mistral.ai/v1", api_key="k", model="mistral-small"
    )
    # Wire type, still an unparseable body -- the point of the test is the
    # fallback on a bad parse, not on the wire shape.
    monkeypatch.setattr(
        classifier, "call_chat_completion",
        lambda *a, **k: LlmCallResult(content="not json at all", usage=None),
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify(
        "Invoice #1842 is due Friday", routing=routing
    )
    assert label == "action_required"
    assert model_version == "heuristic-fallback"


def test_classify_precedence_heuristic_backend_ignores_user_routing(monkeypatch):
    """Row 1 of the frozen precedence matrix: a heuristic-only deployment
    ignores routing entirely -- there's no LLM to route to."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "heuristic")
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)
    credential = LlmCredential(
        provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify(
        "Security alert: new login detected", routing=routing
    )
    assert label == "security_alert"
    assert model_version == "heuristic-v1"


def test_classify_precedence_local_available_wins_over_off_routing(monkeypatch):
    """Row 2 of the matrix: a healthy local encoder's result is returned as
    -is, never downgraded to heuristic just because routing says "off"."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "local")
    monkeypatch.setattr(local_model, "try_predict", lambda text: ("fyi", 0.5, "local rationale", "local:test"))
    routing = ClassificationRouting(mode="off", credential=None)
    label, confidence, rationale, model_version = classify("anything at all", routing=routing)
    assert label == "fyi"
    assert model_version == "local:test"


def test_classify_precedence_auto_backend_local_unavailable_falls_through_to_user(monkeypatch):
    """Row 3 of the matrix: when the local encoder is unavailable, "auto"
    falls through to the LLM path, and mode="user" spends that credential."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "auto")
    monkeypatch.setattr(local_model, "try_predict", lambda text: None)
    credential = LlmCredential(
        provider="mistral", base_url="https://api.mistral.ai/v1", api_key="k", model="mistral-small"
    )
    captured = {}

    def fake_call(cred, *, prompt, user_content, max_tokens, timeout):
        captured["credential"] = cred
        content = json.dumps({"label": "promotional", "confidence": 0.7, "rationale": "sale"})
        return LlmCallResult(content=content, usage=None)

    monkeypatch.setattr(classifier, "call_chat_completion", fake_call)
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify("50% off this weekend!", routing=routing)

    assert label == "promotional"
    assert model_version == "mistral:mistral-small"
    assert captured["credential"] is credential
