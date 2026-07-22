"""Providers-list gating for the optional Outlook/Microsoft OAuth app.

Outlook only shows up in GET /api/v1/auth/providers once all three
MICROSOFT_* credentials are configured -- it's deliberately not
production-required (deploy-gate decision, config.py), so the frontend
must be able to tell whether the "Sign in with Microsoft" flow is usable
before offering it.
"""

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)

PROVIDERS_URL = "/api/v1/auth/providers"


def test_providers_omits_outlook_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", None)
    monkeypatch.setattr(settings, "microsoft_client_secret", None)
    monkeypatch.setattr(settings, "microsoft_redirect_uri", None)

    body = client.get(PROVIDERS_URL).json()

    assert body["providers"] == ["gmail"]


def test_providers_includes_outlook_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", "client-id")
    monkeypatch.setattr(settings, "microsoft_client_secret", "client-secret")
    monkeypatch.setattr(settings, "microsoft_redirect_uri", "https://app.example/callback")

    body = client.get(PROVIDERS_URL).json()

    assert body["providers"] == ["gmail", "outlook"]


def test_providers_requires_all_three_microsoft_credentials(monkeypatch):
    # Partial config (e.g. client id set but secret missing during a botched
    # deploy) must not half-enable the flow -- every route would 503 anyway.
    monkeypatch.setattr(settings, "microsoft_client_id", "client-id")
    monkeypatch.setattr(settings, "microsoft_client_secret", None)
    monkeypatch.setattr(settings, "microsoft_redirect_uri", "https://app.example/callback")

    body = client.get(PROVIDERS_URL).json()

    assert body["providers"] == ["gmail"]
