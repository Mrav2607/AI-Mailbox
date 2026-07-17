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


@pytest.mark.parametrize(
    "alias, value",
    [
        # A negative would quietly behave exactly like the 0 disable switch.
        ("SCHEDULED_SYNC_INTERVAL_SECONDS", -1),
        # Goes to Gmail's maxResults untouched by the ingest route's bounds.
        ("SCHEDULED_SYNC_MAX_RESULTS", 0),
        ("SCHEDULED_SYNC_MAX_RESULTS", 501),
        # 0 means "always stale", not "never".
        ("SYNC_STALE_AFTER_SECONDS", 0),
        ("SYNC_STALE_AFTER_SECONDS", -60),
    ],
)
def test_nonsense_scheduler_settings_are_rejected_at_startup(alias, value):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, **{alias: value})
    # Pin the reason: passing the field name instead of the alias also raises,
    # but for extra_forbidden -- which would make this pass with no bounds at all.
    assert "greater than" in str(exc.value) or "less than" in str(exc.value)


def test_scheduling_can_still_be_disabled_with_zero():
    # 0 is the documented off switch and must stay valid.
    s = Settings(_env_file=None, SCHEDULED_SYNC_INTERVAL_SECONDS=0)
    assert s.scheduled_sync_interval_seconds == 0
