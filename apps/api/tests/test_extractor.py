"""Unit tests for the action extractor: three-way result semantics
(ExtractedAction | NoAction | None), strict response parsing, and the
Gemini call shape -- all offline, no real LLM or network required."""

import json
import sys
from datetime import datetime, timezone

import google.genai
import httpx

from app.services.nlp import extractor
from app.services.nlp.extractor import ExtractedAction, NoAction, extract_action

VALID_PAYLOAD = {
    "has_action": True,
    "kind": "payment",
    "title": "Pay invoice #429",
    "due_at": "2024-03-15T17:00:00Z",
    "due_is_date_only": False,
    "due_raw": "by Friday",
    "amount": 129.99,
    "currency": "USD",
    "confidence": 0.85,
}


class _FakeModels:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        response = type("Response", (), {"text": self._text})()
        return response


class _FakeClient:
    def __init__(self, text=None, exc=None):
        self.models = _FakeModels(text=text, exc=exc)


def _install_fake_client(monkeypatch, text=None, exc=None):
    client = _FakeClient(text=text, exc=exc)
    monkeypatch.setattr(extractor, "_genai_client", lambda: client)
    return client


def _call_extract_action(**overrides):
    kwargs = dict(
        subject="Invoice due",
        sender="billing@example.com",
        snippet="Please pay by Friday",
        body_text="Your invoice #429 is due.",
        received_at=datetime(2024, 3, 10, 12, 0, 0, tzinfo=timezone.utc),
    )
    kwargs.update(overrides)
    return extract_action(**kwargs)


def test_extract_action_returns_none_when_sdk_unavailable(monkeypatch):
    # Blanking the submodule in sys.modules is how Python signals "this
    # import can't succeed" -- but if an earlier test already imported
    # google.genai.errors, `from google.genai import errors` resolves via
    # the already-cached attribute on the parent package and never
    # consults sys.modules at all. Clear that cached attribute too so the
    # None sentinel actually bites regardless of test order.
    monkeypatch.delattr(google.genai, "errors", raising=False)
    monkeypatch.setitem(sys.modules, "google.genai.errors", None)
    assert _call_extract_action() is None


def test_extract_action_returns_none_on_call_failure(monkeypatch):
    _install_fake_client(monkeypatch, exc=httpx.HTTPError("boom"))
    assert _call_extract_action() is None


def test_extract_action_returns_none_on_bad_json(monkeypatch):
    _install_fake_client(monkeypatch, text="not json at all")
    assert _call_extract_action() is None


def test_extract_action_returns_none_on_invalid_kind(monkeypatch):
    payload = {**VALID_PAYLOAD, "kind": "not-a-real-kind"}
    _install_fake_client(monkeypatch, text=json.dumps(payload))
    assert _call_extract_action() is None


def test_extract_action_has_action_false_returns_no_action_not_none(monkeypatch):
    payload = {"has_action": False, "confidence": 0.9}
    _install_fake_client(monkeypatch, text=json.dumps(payload))
    result = _call_extract_action()
    assert isinstance(result, NoAction)
    assert result is not None


def test_extract_action_valid_parse_round_trip(monkeypatch):
    _install_fake_client(monkeypatch, text=json.dumps(VALID_PAYLOAD))
    result = _call_extract_action()
    assert isinstance(result, ExtractedAction)
    assert result.kind == "payment"
    assert result.title == "Pay invoice #429"
    assert result.due_raw == "by Friday"
    assert result.amount == 129.99
    assert result.currency == "USD"
    assert result.confidence == 0.85
    assert result.due_precision == "datetime"
    assert result.due_at == datetime(2024, 3, 15, 17, 0, 0, tzinfo=timezone.utc)
    assert result.model_version == extractor.settings.gemini_model


def test_extract_action_date_only_resolves_to_end_of_day_utc(monkeypatch):
    payload = {**VALID_PAYLOAD, "due_at": "2024-03-15", "due_is_date_only": True}
    _install_fake_client(monkeypatch, text=json.dumps(payload))
    result = _call_extract_action()
    assert isinstance(result, ExtractedAction)
    assert result.due_precision == "date"
    assert result.due_at == datetime(2024, 3, 15, 23, 59, 59, tzinfo=timezone.utc)


def test_extract_action_caps_title_length(monkeypatch):
    long_title = "Pay the invoice right now before it becomes overdue " * 3
    assert len(long_title) > 80
    payload = {**VALID_PAYLOAD, "title": long_title}
    _install_fake_client(monkeypatch, text=json.dumps(payload))
    result = _call_extract_action()
    assert isinstance(result, ExtractedAction)
    assert len(result.title) <= 80


def test_extract_action_prompt_includes_received_at(monkeypatch):
    client = _install_fake_client(monkeypatch, text=json.dumps(VALID_PAYLOAD))
    received_at = datetime(2024, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    _call_extract_action(received_at=received_at)
    contents = client.models.calls[0]["contents"]
    assert received_at.isoformat() in contents


def test_extract_action_passes_response_schema(monkeypatch):
    client = _install_fake_client(monkeypatch, text=json.dumps(VALID_PAYLOAD))
    _call_extract_action()
    config = client.models.calls[0]["config"]
    assert config.response_schema is not None
    assert config.response_mime_type == "application/json"
    schema_properties = config.response_schema["properties"]
    for key in (
        "has_action",
        "kind",
        "title",
        "due_at",
        "due_is_date_only",
        "due_raw",
        "amount",
        "currency",
        "confidence",
    ):
        assert key in schema_properties
