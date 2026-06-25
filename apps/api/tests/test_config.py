"""Config-validation tests: production refuses insecure defaults.

These construct Settings directly with ``_env_file=None`` so the local .env
can't bleed real values into the assertions.
"""

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings

# A complete, valid production config to start from and selectively break.
PROD_OK = dict(
    _env_file=None,
    APP_ENV="production",
    API_SECRET="x" * 40,
    GOOGLE_CLIENT_ID="cid",
    GOOGLE_CLIENT_SECRET="sec",
    GOOGLE_REDIRECT_URI="https://app.example.com/callback",
    TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),
)


def test_dev_allows_insecure_defaults():
    s = Settings(_env_file=None)
    assert s.is_production is False
    assert s.api_secret == "change_me"  # scaffold default is fine in dev


def test_valid_production_config_passes():
    s = Settings(**PROD_OK)
    assert s.is_production is True


def test_production_rejects_default_secret():
    cfg = PROD_OK | {"API_SECRET": "change_me"}
    with pytest.raises(ValidationError, match="API_SECRET"):
        Settings(**cfg)


def test_production_rejects_short_secret():
    cfg = PROD_OK | {"API_SECRET": "too-short"}
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(**cfg)


def test_production_requires_google_credentials():
    cfg = PROD_OK | {"GOOGLE_CLIENT_SECRET": None}
    with pytest.raises(ValidationError, match="GOOGLE_CLIENT_SECRET"):
        Settings(**cfg)


def test_production_requires_token_encryption_key():
    cfg = PROD_OK | {"TOKEN_ENCRYPTION_KEY": None}
    with pytest.raises(ValidationError, match="TOKEN_ENCRYPTION_KEY"):
        Settings(**cfg)


def test_malformed_token_encryption_key_rejected_in_any_env():
    # A non-Fernet key must fail at boot, in dev too -- not at first use.
    with pytest.raises(ValidationError, match="TOKEN_ENCRYPTION_KEY"):
        Settings(_env_file=None, TOKEN_ENCRYPTION_KEY="not-a-valid-fernet-key")


def test_production_reports_all_problems_at_once():
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, APP_ENV="production")
    message = str(exc_info.value)
    assert "API_SECRET" in message
    assert "GOOGLE_CLIENT_ID" in message
    assert "GOOGLE_REDIRECT_URI" in message
    assert "TOKEN_ENCRYPTION_KEY" in message
