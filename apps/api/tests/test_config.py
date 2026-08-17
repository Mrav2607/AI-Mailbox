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
    RESEND_API_KEY="re_test_key",
    FRONTEND_BASE_URL="https://app.example.com",
    EMAIL_FROM="CortexMail <hello@example.com>",
)


def test_dev_allows_insecure_defaults():
    s = Settings(_env_file=None)
    assert s.is_production is False
    assert s.api_secret == "change_me"  # scaffold default is fine in dev


def test_valid_production_config_passes():
    s = Settings(**PROD_OK)
    assert s.is_production is True


def test_frontend_base_url_trailing_slash_is_stripped():
    s = Settings(_env_file=None, FRONTEND_BASE_URL="https://app.example.com/")
    assert s.frontend_base_url == "https://app.example.com"


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


def test_production_requires_resend_and_a_safe_frontend_url():
    with pytest.raises(ValidationError, match="RESEND_API_KEY"):
        Settings(**(PROD_OK | {"RESEND_API_KEY": None}))
    with pytest.raises(ValidationError, match="FRONTEND_BASE_URL"):
        Settings(**(PROD_OK | {"FRONTEND_BASE_URL": "http://app.example.com"}))


def test_production_requires_email_from_to_be_explicitly_set():
    # Drop EMAIL_FROM entirely so Settings falls back to the class default --
    # exactly the unverified-sender-domain case the check exists to catch.
    cfg = {k: v for k, v in PROD_OK.items() if k != "EMAIL_FROM"}
    with pytest.raises(ValidationError, match="EMAIL_FROM"):
        Settings(**cfg)


def test_production_accepts_explicit_email_from():
    s = Settings(**PROD_OK)
    assert s.email_from == "CortexMail <hello@example.com>"


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


# ---------------------------------------------------------------------------
# CLASSIFIER_BACKEND normalization/legacy-mapping/rejection (plan:
# 2026-08-16-classifier-default-honesty §3).
#
# Constructs real `Settings` objects rather than monkeypatching the
# already-booted singleton's attribute -- a singleton patch bypasses this
# validator entirely (it only runs at construction), which is exactly the
# code path these tests exist to prove.
# ---------------------------------------------------------------------------


def test_classifier_backend_defaults_to_auto():
    s = Settings(_env_file=None)
    assert s.classifier_backend == "auto"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("local", "auto"),
        ("gemini", "llm"),
        ("LOCAL", "auto"),
        ("Gemini", "llm"),
    ],
    ids=["local-to-auto", "gemini-to-llm", "uppercase-local", "mixed-case-gemini"],
)
def test_classifier_backend_maps_legacy_spellings(raw, expected):
    # Behaviour-preserving renames (plan §2/§3). The docs moved to `auto` in
    # this branch, but every .env already copied from them says `local`, and
    # those deployments have to keep booting -- not just keep classifying
    # correctly.
    s = Settings(_env_file=None, CLASSIFIER_BACKEND=raw)
    assert s.classifier_backend == expected


def test_classifier_backend_blank_defaults_to_auto():
    s = Settings(_env_file=None, CLASSIFIER_BACKEND="")
    assert s.classifier_backend == "auto"


@pytest.mark.parametrize("raw", ["auto ", " auto", "\tauto\n", " AUTO "])
def test_classifier_backend_tolerates_surrounding_whitespace(raw):
    # A .env or compose line picks up a trailing space very easily. Without
    # the strip that boots to a hard ValueError on a value the operator would
    # swear is correct, which is a miserable way to find out.
    s = Settings(_env_file=None, CLASSIFIER_BACKEND=raw)
    assert s.classifier_backend == "auto"


@pytest.mark.parametrize("raw", ["   ", "\t", "\n"])
def test_classifier_backend_whitespace_only_defaults_to_auto(raw):
    # Same fallback a truly blank value gets -- whitespace-only is blank as
    # far as an operator is concerned, so it must not raise.
    s = Settings(_env_file=None, CLASSIFIER_BACKEND=raw)
    assert s.classifier_backend == "auto"


def test_classifier_backend_strips_before_mapping_a_legacy_alias():
    # The strip has to happen before the alias lookup, not after, or a padded
    # legacy value misses the map and falls through to the reject branch.
    s = Settings(_env_file=None, CLASSIFIER_BACKEND=" local ")
    assert s.classifier_backend == "auto"


@pytest.mark.parametrize("raw", ["HEURISTIC", "Auto", "LLM"])
def test_classifier_backend_case_insensitive_for_canonical_values(raw):
    s = Settings(_env_file=None, CLASSIFIER_BACKEND=raw)
    assert s.classifier_backend == raw.lower()


def test_classifier_backend_rejects_unknown_value():
    with pytest.raises(ValidationError, match="CLASSIFIER_BACKEND"):
        Settings(_env_file=None, CLASSIFIER_BACKEND="banana")


def test_classifier_backend_rejects_local_then_llm_as_a_global_value():
    # local_then_llm is per-run only (a `backend=` override on a single
    # request/backfill) -- never a deployment default. Accepting it here
    # would mean every classify() call silently loses the global-default
    # opt-in-wins-first semantics "auto" carries (plan's Wave plan section).
    with pytest.raises(ValidationError, match="CLASSIFIER_BACKEND"):
        Settings(_env_file=None, CLASSIFIER_BACKEND="local_then_llm")
