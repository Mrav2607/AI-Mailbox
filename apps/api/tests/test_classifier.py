"""Unit tests for the classifier: heuristic rules + backend dispatch/fallback
(local/llm/heuristic), all offline -- no LLM or network required."""

import json

import pytest

from app.services.nlp.classifier import (
    LABELS,
    ClassificationAttempt,
    _heuristic_classify,
    classify,
    classify_with_usage,
)
from app.services.nlp.llm_client import LlmCallError, LlmCallResult, LlmUsage
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

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
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
    monkeypatch.setattr(local_model, "_state", ("tok", "model", ["a"], "cpu", "v1", None))
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

    state = (fake_tokenizer, lambda **enc: FakeOutput(), ["a", "b", "c"], "cpu", "test", None)
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
        local_model, "_state", ("tok", "model", ["a"], "cpu", "test", None)
    )
    monkeypatch.setattr(local_model, "_infer_slots", Semaphore(0))
    monkeypatch.setattr(local_model, "_SLOT_TIMEOUT_S", 0.01)

    assert local_model.try_predict("hello") is None


# ---------------------------------------------------------------------------
# calibration.json loading/validation and application before softmax
# (plan: docs/plans/2026-08-07-model-v21-calibration-plan.md §2).
# ---------------------------------------------------------------------------


# The frozen v2.1 schema is pinned to exactly six labels -- these tests use
# the real served label order (`app.services.nlp.classifier.LABELS`) rather
# than an arbitrary short list, so the six-label pin is exercised for real.
_SIX_LABELS = list(LABELS)
_SIX_LABELS_SWAPPED = [_SIX_LABELS[1], _SIX_LABELS[0], *_SIX_LABELS[2:]]  # same set, wrong order


def _write_model_dir(tmp_path, *, required=False, calibration=None):
    """Write just the pieces `_load_calibration` reads -- no real HF model
    files needed since it never touches the tokenizer/model."""
    config = {"calibration_required": True} if required else {}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if calibration is not None:
        (tmp_path / "calibration.json").write_text(json.dumps(calibration), encoding="utf-8")
    return tmp_path


def test_load_calibration_absent_file_is_identity(tmp_path):
    # No marker, no file -- legacy v1/v2 dirs get plain identity.
    from app.services.nlp import local_model

    model_dir = _write_model_dir(tmp_path, required=False)
    assert local_model._load_calibration(model_dir, _SIX_LABELS, torch=None, device="cpu") is None


def test_load_calibration_required_but_missing_raises(tmp_path):
    # A v2.1-marked dir missing its calibration.json must fail loudly, not
    # silently serve uncalibrated confidences under the v2.1 label.
    from app.services.nlp import local_model

    model_dir = _write_model_dir(tmp_path, required=True)
    with pytest.raises(ValueError, match="calibration_required"):
        local_model._load_calibration(model_dir, _SIX_LABELS, torch=None, device="cpu")


def test_load_calibration_rejects_non_six_label_schema_even_when_labels_match(tmp_path):
    # The frozen schema is pinned to exactly six labels, full stop -- a
    # calibration file must be rejected for a hypothetical 3-label model
    # even when its "labels" list matches the served labels exactly.
    from app.services.nlp import local_model

    three_labels = _SIX_LABELS[:3]
    calibration = {
        "schema": 1,
        "kind": "temperature",
        "labels": three_labels,
        "params": {"T": 2.0},
    }
    model_dir = _write_model_dir(tmp_path, calibration=calibration)
    with pytest.raises(ValueError, match="exactly"):
        local_model._load_calibration(model_dir, three_labels, torch=None, device="cpu")


@pytest.mark.parametrize(
    "calibration, match",
    [
        ({"schema": 2, "kind": "temperature", "labels": _SIX_LABELS, "params": {"T": 2.0}}, "schema"),
        ({"schema": True, "kind": "temperature", "labels": _SIX_LABELS, "params": {"T": 2.0}}, "schema"),
        ({"schema": 1, "kind": "bogus", "labels": _SIX_LABELS, "params": {}}, "kind"),
        ({"schema": 1, "kind": "temperature", "labels": _SIX_LABELS_SWAPPED, "params": {"T": 2.0}}, "labels"),
        ({"schema": 1, "kind": "temperature", "labels": _SIX_LABELS, "params": {"T": 0.0}}, "temperature"),
        ({"schema": 1, "kind": "temperature", "labels": _SIX_LABELS, "params": {"T": -1.0}}, "temperature"),
        ({"schema": 1, "kind": "temperature", "labels": _SIX_LABELS, "params": {"T": float("nan")}}, "temperature"),
        ({"schema": 1, "kind": "temperature", "labels": _SIX_LABELS, "params": {"T": float("inf")}}, "temperature"),
        (
            {
                "schema": 1,
                "kind": "vector",
                "labels": _SIX_LABELS,
                "params": {"w": [1.0, float("nan"), 1.0, 1.0, 1.0, 1.0], "b": [0.0] * 6},
            },
            "vector params",
        ),
        (
            {
                "schema": 1,
                "kind": "vector",
                "labels": _SIX_LABELS,
                "params": {"w": [1.0, 1.0], "b": [0.0] * 6},
            },
            "vector params",
        ),
        (
            {
                "schema": 1,
                "kind": "vector",
                "labels": _SIX_LABELS,
                "params": {"w": [1.0, -1.0, 1.0, 1.0, 1.0, 1.0], "b": [0.0] * 6},
            },
            "non-positive",
        ),
    ],
    ids=[
        "bad-schema",
        "schema-boolean-true",
        "bad-kind",
        "label-mismatch",
        "temperature-zero",
        "temperature-negative",
        "temperature-nan",
        "temperature-infinity",
        "vector-nan-w",
        "vector-wrong-length-w",
        "vector-nonpositive-w",
    ],
)
def test_load_calibration_malformed_raises(tmp_path, calibration, match):
    # Any schema/param violation is a definitive load failure -- never
    # silently ignored.
    from app.services.nlp import local_model

    model_dir = _write_model_dir(tmp_path, calibration=calibration)
    with pytest.raises(ValueError, match=match):
        local_model._load_calibration(model_dir, _SIX_LABELS, torch=None, device="cpu")


def test_warmup_marks_unavailable_when_calibration_fails_validation(monkeypatch, tmp_path):
    # Wiring check: a `_load_calibration` failure propagates through `_load`'s
    # existing exception handling into `_unavailable`, exactly like any other
    # definitive load failure (missing deps, corrupt files, ...).
    from app.services.nlp import local_model

    local_model.reset()
    model_dir = _write_model_dir(tmp_path, required=True)  # marked required, no calibration.json

    monkeypatch.setattr(
        local_model,
        "_load",
        lambda: local_model._load_calibration(model_dir, _SIX_LABELS, torch=None, device="cpu"),
    )
    local_model.warmup()
    assert local_model._unavailable is True
    local_model.reset()


def test_try_predict_temperature_calibration_changes_confidence_not_argmax(monkeypatch):
    # Same fake-logits double as the fast-path test above. Softening with
    # T=2 must shrink the winning confidence but never flip which label wins
    # -- scalar temperature preserves argmax by construction.
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

    labels = ["a", "b", "c"]
    uncalibrated_state = (fake_tokenizer, lambda **enc: FakeOutput(), labels, "cpu", "test", None)
    calibrated_state = (
        fake_tokenizer,
        lambda **enc: FakeOutput(),
        labels,
        "cpu",
        "test",
        {"kind": "temperature", "T": 2.0},
    )

    monkeypatch.setattr(local_model, "_state", uncalibrated_state)
    uncal_label, uncal_conf, *_ = local_model.try_predict("hello")

    monkeypatch.setattr(local_model, "_state", calibrated_state)
    cal_label, cal_conf, *_ = local_model.try_predict("hello")

    assert cal_label == uncal_label == "b"
    assert cal_conf < uncal_conf
    local_model.reset()


def test_try_predict_vector_calibration_is_numerically_correct(monkeypatch):
    # Fixed logits/w/b -- expected transformed logits, softmax, and argmax
    # are hand-computed (not by re-deriving the implementation's own
    # formula) so this actually catches an order-of-operations bug, not
    # just mirrors it.
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

    w = [1.0, 0.5, 2.0]
    b = [0.0, -1.0, 0.5]
    # transformed = logits * w + b = [0.1, 1.5, 0.9]; softmax peaks at index 1
    # (label "b") with confidence 0.557 -- computed independently in Python.
    state = (
        fake_tokenizer,
        lambda **enc: FakeOutput(),
        ["a", "b", "c"],
        "cpu",
        "test",
        {"kind": "vector", "w": torch.tensor(w), "b": torch.tensor(b)},
    )
    monkeypatch.setattr(local_model, "_state", state)

    label, confidence, *_ = local_model.try_predict("hello")
    assert label == "b"
    assert confidence == pytest.approx(0.557, abs=1e-3)
    local_model.reset()


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


def test_classify_backend_gemini_is_an_alias_for_llm(monkeypatch):
    """"gemini" was the old name for the LLM path. Deployments still carry it
    in their .env, so it has to keep landing on the same branch as "llm" --
    if it ever fell through to the local/auto branch instead, BYOK
    classification would silently stop running on those boxes."""
    from app.services.nlp import classifier

    reached = []

    def _spy(text, routing=None, local_tried=False):
        reached.append(text)
        return ClassificationAttempt(
            verdict=("fyi", 0.9, "spy", "spy-v1"),
            provider_call_succeeded=True,
            usage=None,
        )

    monkeypatch.setattr(classifier, "_classify_llm", _spy)

    # Both ways in: the configured default, and the explicit per-call override
    # that the backfill route passes. They dispatch through the same lookup,
    # but only the override path is reachable from the API, so pin both.
    for backend in ("gemini", "llm"):
        monkeypatch.setattr(classifier.settings, "classifier_backend", backend)
        assert classify("Can you review this?")[3] == "spy-v1", f"configured {backend}"

    monkeypatch.setattr(classifier.settings, "classifier_backend", "heuristic")
    for backend in ("gemini", "llm"):
        assert classify("Can you review this?", backend=backend)[3] == "spy-v1", (
            f"override {backend}"
        )

    assert len(reached) == 4


def test_classify_routing_none_and_server_are_byte_identical_no_key(monkeypatch):
    """With no gemini_api_key configured, routing=None and mode="server"
    must both fall back to heuristic identically -- BYOK is additive."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
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

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
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

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
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

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
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

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(classifier, "_genai_client", _explode)
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)

    routing = ClassificationRouting(mode="user", credential=None)
    label, confidence, rationale, model_version = classify(
        "Security alert: new login detected", routing=routing
    )
    assert label == "security_alert"
    assert model_version == "heuristic-v1"  # direct heuristic, not "-fallback"


def test_classify_routing_user_mode_falls_back_to_heuristic_on_call_error(monkeypatch):
    # Forces the encoder unavailable so this pins the "nothing can serve"
    # tail of D2's fallback chain -- case 9 covers the encoder-available half.
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(local_model, "try_predict", lambda text: None)
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
    # Forces the encoder unavailable so this pins the "nothing can serve"
    # tail of D2's fallback chain -- case 10 covers the encoder-available half.
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(local_model, "try_predict", lambda text: None)
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


# ---------------------------------------------------------------------------
# ClassificationAttempt contract: classify_with_usage() and
# provider_call_succeeded / usage on every path (plan §3's wrapper contract).
# ---------------------------------------------------------------------------


def test_classify_with_usage_verdict_matches_classify_for_every_routing_mode(monkeypatch):
    """Regression guard for the refactor: classify() must still return
    exactly what classify_with_usage().verdict returns, for every routing
    mode -- the whole point of `classify` being a one-line wrapper."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(classifier.settings, "gemini_api_key", None)

    credential = LlmCredential(
        provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
    )
    monkeypatch.setattr(
        classifier, "call_chat_completion",
        lambda *a, **k: LlmCallResult(
            content=json.dumps({"label": "fyi", "confidence": 0.5, "rationale": "r"}), usage=None
        ),
    )

    for routing in (
        None,
        ClassificationRouting(mode="server", credential=None),
        ClassificationRouting(mode="off", credential=None),
        ClassificationRouting(mode="user", credential=credential),
    ):
        text = "Can you review this?"
        assert classify(text, routing=routing) == classify_with_usage(text, routing=routing).verdict


def test_classify_with_usage_heuristic_backend_never_touched_provider(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "heuristic")
    attempt = classify_with_usage("Security alert: new login detected")
    assert attempt.provider_call_succeeded is False
    assert attempt.usage is None
    assert attempt.verdict[0] == "security_alert"
    assert attempt.llm_attempted is False
    assert attempt.fallback_used is False
    assert attempt.failure_category is None


def test_classify_with_usage_local_backend_never_touched_provider(monkeypatch):
    """The predicate trap the plan calls out by name: a CLASSIFIER_BACKEND=
    local run must report llm_attempted=False / fallback_used=False -- it
    never even considered an LLM, so it must never be counted as a degraded
    run just because routing.mode happens to say "user"."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "local")
    monkeypatch.setattr(
        local_model, "try_predict", lambda text: ("fyi", 0.5, "local rationale", "local:test")
    )
    attempt = classify_with_usage("anything at all")
    assert attempt.provider_call_succeeded is False
    assert attempt.usage is None
    assert attempt.verdict == ("fyi", 0.5, "local rationale", "local:test")
    assert attempt.llm_attempted is False
    assert attempt.fallback_used is False
    assert attempt.failure_category is None


def test_classify_with_usage_routing_off_never_touched_provider(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(classifier, "_genai_client", _explode)
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)

    routing = ClassificationRouting(mode="off", credential=None)
    attempt = classify_with_usage("Can you help?", routing=routing)
    assert attempt.provider_call_succeeded is False
    assert attempt.usage is None


def test_classify_with_usage_user_mode_no_credential_never_touched_provider(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(classifier, "_genai_client", _explode)
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)

    routing = ClassificationRouting(mode="user", credential=None)
    attempt = classify_with_usage("Security alert: new login detected", routing=routing)
    assert attempt.provider_call_succeeded is False
    assert attempt.usage is None


def test_classify_with_usage_user_mode_call_error_never_touched_provider(monkeypatch):
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    credential = LlmCredential(
        provider="groq", base_url="https://api.groq.com/openai/v1", api_key="k", model="llama"
    )

    def failing_call(*args, **kwargs):
        raise LlmCallError("connection_failed", None)

    monkeypatch.setattr(classifier, "call_chat_completion", failing_call)
    routing = ClassificationRouting(mode="user", credential=credential)
    attempt = classify_with_usage("Security alert: new login detected", routing=routing)
    assert attempt.provider_call_succeeded is False
    assert attempt.usage is None


def test_classify_with_usage_user_mode_real_preflight_rejection_never_attempted_llm(monkeypatch):
    """A REAL destination-policy rejection (not a stubbed category), same
    fix and same pattern as test_extractor.py's counterpart -- drives the
    actual call_chat_completion (not classifier.call_chat_completion mocked
    away) through a fake pin_custom_destination that raises
    DestinationRejected. blocked_by_policy rejects BEFORE any request
    leaves the process, so llm_attempted must be False even though a
    fallback genuinely did serve the verdict (Codex review)."""
    from app.services.nlp import classifier, local_model
    from app.services.nlp import llm_client as llm_client_module
    from app.services.nlp.providers import DestinationRejected

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(local_model, "try_predict", lambda text: None)

    def fake_pin(url):
        raise DestinationRejected("destination_rejected", "nope")

    monkeypatch.setattr(llm_client_module, "pin_custom_destination", fake_pin)
    credential = LlmCredential(
        provider="custom", base_url="https://ollama.example.com/v1", api_key="k", model="llama3"
    )
    routing = ClassificationRouting(mode="user", credential=credential)

    attempt = classify_with_usage("Security alert: new login detected", routing=routing)

    assert attempt.verdict[3] == "heuristic-fallback"
    assert attempt.provider_call_succeeded is False
    assert attempt.llm_attempted is False
    assert attempt.fallback_used is True
    assert attempt.failure_category == "blocked_by_policy"


def test_classify_with_usage_user_mode_success_carries_usage_through(monkeypatch):
    """The BYOK call succeeded and parsed cleanly -- the recorded call and
    the reported tokens both count."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    credential = LlmCredential(
        provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
    )
    usage = LlmUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    content = json.dumps({"label": "spam", "confidence": 0.9, "rationale": "scam"})
    monkeypatch.setattr(
        classifier, "call_chat_completion",
        lambda *a, **k: LlmCallResult(content=content, usage=usage),
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    attempt = classify_with_usage("You won a prize!", routing=routing)

    assert attempt.provider_call_succeeded is True
    assert attempt.usage is usage
    assert attempt.verdict == ("spam", 0.9, "scam", "openai:gpt-4o-mini")


def test_classify_with_usage_user_mode_counts_the_call_even_when_content_is_unparseable(monkeypatch):
    """The one a future refactor is most likely to break: the provider
    answered (and billed the user) even though its content didn't parse, so
    the provider-reported usage still has to survive on `attempt.usage` --
    regardless of whether the fallback verdict ends up served by the local
    encoder or (forced unavailable here) keyword rules, per D3."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(local_model, "try_predict", lambda text: None)
    credential = LlmCredential(
        provider="mistral", base_url="https://api.mistral.ai/v1", api_key="k", model="mistral-small"
    )
    usage = LlmUsage(prompt_tokens=50, completion_tokens=None, total_tokens=None)
    monkeypatch.setattr(
        classifier, "call_chat_completion",
        lambda *a, **k: LlmCallResult(content="not json at all", usage=usage),
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    attempt = classify_with_usage("Invoice #1842 is due Friday", routing=routing)

    assert attempt.provider_call_succeeded is True
    assert attempt.usage is usage
    assert attempt.verdict[3] == "heuristic-fallback"


def test_classify_with_usage_server_path_success_reports_call_but_no_usage(monkeypatch):
    """Server (operator-paid) path: the call counts, but usage is
    deliberately left None -- operator-paid usage is a v1 non-goal and
    `response.usage_metadata` is never parsed here."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(classifier.settings, "gemini_api_key", "server-key")
    monkeypatch.setattr(classifier.settings, "gemini_model", "gemini-2.5-flash")
    monkeypatch.setattr(
        classifier, "_genai_client", lambda: _fake_genai_response(label="fyi")
    )

    for routing in (None, ClassificationRouting(mode="server", credential=None)):
        attempt = classify_with_usage("hi there", routing=routing)
        assert attempt.provider_call_succeeded is True
        assert attempt.usage is None
        assert attempt.verdict[3] == "gemini-2.5-flash"
        assert attempt.llm_attempted is True
        assert attempt.fallback_used is False
        assert attempt.failure_category is None


def test_classify_with_usage_server_path_no_key_never_attempted_llm(monkeypatch):
    """No operator key configured: the server path stops before ever trying
    to reach Gemini -- llm_attempted must be False, not just
    provider_call_succeeded."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(classifier.settings, "gemini_api_key", None)

    attempt = classify_with_usage("Can you review this?")
    assert attempt.llm_attempted is False
    assert attempt.fallback_used is False
    assert attempt.failure_category is None


def test_classify_with_usage_server_path_call_failure_reports_attempted_fallback(monkeypatch):
    """A Gemini call that goes out and fails (rate limit, timeout, ...) is a
    real attempt-then-fallback -- unlike an unconfigured key or a missing
    SDK, which never issue a request at all."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(classifier.settings, "gemini_api_key", "server-key")

    class _ExplodingModels:
        def generate_content(self, **kwargs):
            raise TimeoutError("gemini took too long")

    class _ExplodingClient:
        models = _ExplodingModels()

    monkeypatch.setattr(classifier, "_genai_client", lambda: _ExplodingClient())

    attempt = classify_with_usage("Can you review this?")
    assert attempt.verdict[3] == "heuristic-fallback"
    assert attempt.llm_attempted is True
    assert attempt.fallback_used is True
    # No structured category taxonomy exists for the native genai path (that
    # taxonomy is BYOK-only, from llm_client.LlmCallError).
    assert attempt.failure_category is None


def test_classify_with_usage_server_path_malformed_response_is_invalid_response(monkeypatch):
    """A Gemini reply that doesn't parse still counts as an attempt (the call
    itself succeeded) and is categorized invalid_response, same taxonomy as
    the BYOK path's malformed-response case."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    monkeypatch.setattr(classifier.settings, "gemini_api_key", "server-key")

    class _BadResponse:
        text = "not json at all"

    class _BadModels:
        def generate_content(self, **kwargs):
            return _BadResponse()

    class _BadClient:
        models = _BadModels()

    monkeypatch.setattr(classifier, "_genai_client", lambda: _BadClient())

    attempt = classify_with_usage("Can you review this?")
    assert attempt.verdict[3] == "heuristic-fallback"
    assert attempt.provider_call_succeeded is True
    assert attempt.llm_attempted is True
    assert attempt.fallback_used is True
    assert attempt.failure_category == "invalid_response"


# ---------------------------------------------------------------------------
# BYOK classification precedence plan (2026-08-04), §5 cases 1-14: explicit
# vs. default `backend`, D1a's "explicit local never reaches an LLM", and
# D2/D2a's local-encoder-before-heuristic failure fallback gated by
# `local_tried` so the encoder is never attempted twice in one call.
# ---------------------------------------------------------------------------


def test_classify_default_backend_user_mode_never_consults_local_encoder(monkeypatch):
    """Case 1: the global default (backend not passed for this run) sends an
    opted-in user straight to their key -- the local encoder is never even
    asked, so a deployment defaulting to local/auto (today's
    CLASSIFIER_BACKEND default) still honors the opt-in instead of
    classifying that mail for free."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "local")
    monkeypatch.setattr(local_model, "try_predict", _explode)

    credential = LlmCredential(
        provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
    )
    content = json.dumps({"label": "fyi", "confidence": 0.8, "rationale": "r"})
    monkeypatch.setattr(
        classifier, "call_chat_completion", lambda *a, **k: LlmCallResult(content=content, usage=None)
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify("hi", routing=routing)
    assert model_version == "openai:gpt-4o-mini"


def test_classify_default_backend_server_mode_uses_encoder(monkeypatch):
    """Case 2 (regression guard, already holds today): default backend +
    mode="server" still prefers a healthy local encoder over the operator's
    key."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "local")
    monkeypatch.setattr(
        local_model, "try_predict", lambda text: ("fyi", 0.5, "local rationale", "local:test")
    )
    monkeypatch.setattr(classifier, "_genai_client", _explode)

    routing = ClassificationRouting(mode="server", credential=None)
    label, confidence, rationale, model_version = classify("hi", routing=routing)
    assert model_version == "local:test"


def test_classify_default_backend_off_mode_uses_encoder(monkeypatch):
    """Case 3 (regression guard, already holds today): default backend +
    mode="off" still uses the encoder and never calls an LLM."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "local")
    monkeypatch.setattr(
        local_model, "try_predict", lambda text: ("fyi", 0.5, "local rationale", "local:test")
    )
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)
    monkeypatch.setattr(classifier, "_genai_client", _explode)

    routing = ClassificationRouting(mode="off", credential=None)
    label, confidence, rationale, model_version = classify("hi", routing=routing)
    assert model_version == "local:test"


def test_classify_default_backend_no_routing_uses_encoder(monkeypatch):
    """Case 4 (regression guard, pre-BYOK parity): default backend +
    routing=None (a caller that never passes routing at all) still uses the
    encoder."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(classifier.settings, "classifier_backend", "local")
    monkeypatch.setattr(
        local_model, "try_predict", lambda text: ("fyi", 0.5, "local rationale", "local:test")
    )
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)

    label, confidence, rationale, model_version = classify("hi")
    assert model_version == "local:test"


def test_classify_explicit_local_user_mode_encoder_available_never_spends_key(monkeypatch):
    """Case 5: explicit backend="local" always tries the encoder first, even
    for an opted-in user -- it's available here, so the key is never
    touched."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(
        local_model, "try_predict", lambda text: ("fyi", 0.5, "local rationale", "local:test")
    )
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)

    credential = LlmCredential(
        provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify("hi", backend="local", routing=routing)
    assert model_version == "local:test"


def test_classify_explicit_local_user_mode_encoder_unavailable_stops_at_heuristic(monkeypatch):
    """Case 6 (D1a -- the blocker case v2's test set missed): explicit
    backend="local" with an unavailable encoder must stop at keyword rules,
    opted-in user or not -- it must NEVER reach an LLM. model_version is the
    direct "heuristic-v1" stamp, not "-fallback", since no call was ever
    attempted."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(local_model, "try_predict", lambda text: None)
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)
    monkeypatch.setattr(classifier, "_genai_client", _explode)

    credential = LlmCredential(
        provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify(
        "Security alert: new login detected", backend="local", routing=routing
    )
    assert label == "security_alert"
    assert model_version == "heuristic-v1"


def test_classify_explicit_auto_encoder_unavailable_user_mode_reaches_llm(monkeypatch):
    """Case 7: unlike explicit "local", explicit backend="auto" retains the
    local-first, LLM-on-failure order. With the encoder unavailable, it
    falls through to the user's key -- and threads local_tried=True into
    the LLM dispatch, since the encoder was already tried once (D2a). Spies
    on `_classify_llm`'s own call signature rather than the end-to-end
    result: with the encoder already unavailable, main's dispatch and this
    fix's dispatch reach the LLM the same way on a *success*, so only the
    `local_tried` flag distinguishes them -- a plain result-equality
    assertion here would pass on unfixed code too and prove nothing."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(local_model, "try_predict", lambda text: None)
    credential = LlmCredential(
        provider="mistral", base_url="https://api.mistral.ai/v1", api_key="k", model="mistral-small"
    )
    captured = {}

    def spy(text, routing=None, local_tried=False):
        captured["local_tried"] = local_tried
        captured["routing"] = routing
        return ClassificationAttempt(
            verdict=("promotional", 0.7, "sale", "spy-v1"),
            provider_call_succeeded=True,
            usage=None,
        )

    monkeypatch.setattr(classifier, "_classify_llm", spy)
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify(
        "50% off this weekend!", backend="auto", routing=routing
    )
    assert model_version == "spy-v1"
    assert captured["routing"] is routing
    assert captured["local_tried"] is True


def test_classify_explicit_llm_user_mode_calls_the_key(monkeypatch):
    """Case 8 (regression guard, already holds today): explicit
    backend="llm" + mode="user" behaves exactly as before -- straight to the
    user's key."""
    from app.services.nlp import classifier

    monkeypatch.setattr(classifier, "_genai_client", _explode)
    credential = LlmCredential(
        provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
    )
    content = json.dumps({"label": "spam", "confidence": 0.9, "rationale": "scam"})
    monkeypatch.setattr(
        classifier, "call_chat_completion", lambda *a, **k: LlmCallResult(content=content, usage=None)
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify(
        "You won a prize!", backend="llm", routing=routing
    )
    assert label == "spam"
    assert model_version == "openai:gpt-4o-mini"


def test_classify_user_key_call_error_falls_back_to_encoder_not_heuristic(monkeypatch):
    """Case 9 (D2): a transient provider error on a BYOK call now falls back
    to the local encoder, not straight to keyword rules -- an opted-in user
    with a flaky provider shouldn't get worse labels than someone who never
    opted in. The encoder's model_version carries the `+fallback` provenance
    marker (plan: 2026-08-14-llm-failure-visibility) -- without it this row
    would be byte-identical to a healthy local-backend run."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(
        local_model, "try_predict", lambda text: ("fyi", 0.6, "local rationale", "local:test")
    )

    def failing_call(*args, **kwargs):
        raise LlmCallError("connection_failed", None)

    monkeypatch.setattr(classifier, "call_chat_completion", failing_call)
    credential = LlmCredential(
        provider="groq", base_url="https://api.groq.com/openai/v1", api_key="k", model="llama"
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    attempt = classify_with_usage(
        "Security alert: new login detected", backend="llm", routing=routing
    )
    assert attempt.verdict == ("fyi", 0.6, "local rationale", "local:test+fallback")
    assert attempt.provider_call_succeeded is False  # the call never reached the provider
    assert attempt.usage is None
    assert attempt.llm_attempted is True
    assert attempt.fallback_used is True
    assert attempt.failure_category == "connection_failed"


def test_classify_user_key_malformed_response_falls_back_to_encoder(monkeypatch):
    """Case 10 (D2): a malformed BYOK response also falls back to the local
    encoder before keyword rules. provider_call_succeeded/usage still
    reflect that the provider answered and billed the user (D3) -- only the
    verdict's source changes. The encoder's model_version carries the
    `+fallback` provenance marker, and the failure is categorized as
    `invalid_response` even though it never raised an `LlmCallError`."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(
        local_model, "try_predict",
        lambda text: ("action_required", 0.6, "local rationale", "local:test"),
    )
    usage = LlmUsage(prompt_tokens=50, completion_tokens=None, total_tokens=None)
    monkeypatch.setattr(
        classifier, "call_chat_completion",
        lambda *a, **k: LlmCallResult(content="not json at all", usage=usage),
    )
    credential = LlmCredential(
        provider="mistral", base_url="https://api.mistral.ai/v1", api_key="k", model="mistral-small"
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    attempt = classify_with_usage("Invoice #1842 is due Friday", backend="llm", routing=routing)
    assert attempt.verdict == ("action_required", 0.6, "local rationale", "local:test+fallback")
    assert attempt.provider_call_succeeded is True
    assert attempt.usage is usage
    assert attempt.llm_attempted is True
    assert attempt.fallback_used is True
    assert attempt.failure_category == "invalid_response"


def test_classify_user_key_failure_with_encoder_unavailable_uses_heuristic_fallback(monkeypatch):
    """Case 11 (regression guard, already holds today): when the encoder
    can't serve either, the failure paths land on keyword rules exactly as
    before, stamped "heuristic-fallback" -- the encoder attempt happened but
    came up empty, unlike case 6's "never even attempted" heuristic-v1."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(local_model, "try_predict", lambda text: None)

    def failing_call(*args, **kwargs):
        raise LlmCallError("connection_failed", None)

    monkeypatch.setattr(classifier, "call_chat_completion", failing_call)
    credential = LlmCredential(
        provider="groq", base_url="https://api.groq.com/openai/v1", api_key="k", model="llama"
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify(
        "Security alert: new login detected", backend="llm", routing=routing
    )
    assert label == "security_alert"
    assert model_version == "heuristic-fallback"


def test_classify_explicit_auto_failure_path_tries_local_encoder_at_most_once(monkeypatch):
    """Case 12 (D2a): under explicit backend="auto" the encoder is already
    attempted once before the LLM call. If the LLM call then also fails, the
    failure handler must NOT attempt the encoder a second time -- its load
    path is expensive (unbounded torch/transformers import + model load) and
    D2a says never twice per call."""
    from app.services.nlp import classifier, local_model

    calls = []

    def counting_try_predict(text):
        calls.append(1)
        return None

    monkeypatch.setattr(local_model, "try_predict", counting_try_predict)

    def failing_call(*args, **kwargs):
        raise LlmCallError("connection_failed", None)

    monkeypatch.setattr(classifier, "call_chat_completion", failing_call)
    credential = LlmCredential(
        provider="mistral", base_url="https://api.mistral.ai/v1", api_key="k", model="mistral-small"
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify(
        "Security alert: new login detected", backend="auto", routing=routing
    )
    assert model_version == "heuristic-fallback"
    assert len(calls) == 1


def test_classify_with_usage_provider_call_succeeded_matches_whether_provider_answered(monkeypatch):
    """Case 13 (D3, regression guard): provider_call_succeeded/usage reflect
    whether the LLM call reached the provider, not what ultimately produced
    the verdict. usage.py returns early when provider_call_succeeded is
    False, so an LlmCallError never emits a usage row while a malformed
    response does -- unaffected by whether the encoder or keyword rules
    served the fallback label."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(local_model, "try_predict", lambda text: None)
    credential = LlmCredential(
        provider="groq", base_url="https://api.groq.com/openai/v1", api_key="k", model="llama"
    )
    routing = ClassificationRouting(mode="user", credential=credential)

    def failing_call(*args, **kwargs):
        raise LlmCallError("connection_failed", None)

    monkeypatch.setattr(classifier, "call_chat_completion", failing_call)
    attempt = classify_with_usage(
        "Security alert: new login detected", backend="llm", routing=routing
    )
    assert attempt.provider_call_succeeded is False
    assert attempt.usage is None

    usage = LlmUsage(prompt_tokens=50, completion_tokens=None, total_tokens=None)
    monkeypatch.setattr(
        classifier, "call_chat_completion",
        lambda *a, **k: LlmCallResult(content="not json at all", usage=usage),
    )
    attempt = classify_with_usage("Invoice #1842 is due Friday", backend="llm", routing=routing)
    assert attempt.provider_call_succeeded is True
    assert attempt.usage is usage


def test_classify_explicit_auto_user_mode_encoder_available_wins_over_key(monkeypatch):
    """Case 14: explicit backend="auto" with a HEALTHY encoder wins over an
    opted-in user's key -- this is the case that distinguishes the default
    path (case 1, where the opt-in always wins) from an explicit per-run
    "auto" override, which retains the local-first order even for an
    opted-in user. The existing suite never covered the encoder-AVAILABLE
    half of this; it only had the encoder-unavailable fallthrough."""
    from app.services.nlp import classifier, local_model

    monkeypatch.setattr(
        local_model, "try_predict", lambda text: ("fyi", 0.5, "local rationale", "local:test")
    )
    monkeypatch.setattr(classifier, "call_chat_completion", _explode)

    credential = LlmCredential(
        provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-4o-mini"
    )
    routing = ClassificationRouting(mode="user", credential=credential)
    label, confidence, rationale, model_version = classify("hi", backend="auto", routing=routing)
    assert model_version == "local:test"
