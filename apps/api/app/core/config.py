from pathlib import Path

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator


# api_secret signs HS256 JWTs; HMAC-SHA256 wants a key of at least this many
# bytes (RFC 7518 3.2), and "change_me" is the insecure scaffold default.
_MIN_SECRET_BYTES = 32
_PLACEHOLDER_SECRET = "change_me"
# Environments treated as non-production, where the insecure defaults are fine.
_DEV_ENVS = {"dev", "development", "local", "test", "testing", "ci"}

# The only signing algorithms we support. api_secret is a shared HMAC secret,
# so asymmetric algorithms (RS*/ES*) can't work here, and "none" would disable
# signature checks entirely -- reject anything outside this set at boot.
_ALLOWED_JWT_ALGORITHMS = {"HS256", "HS384", "HS512"}


_CONFIG_PATH = Path(__file__).resolve()
# Look for a .env at the repo root (dev layout) and at the api package root.
# Depths differ between the local checkout and the container, so skip any
# index that doesn't exist instead of raising IndexError.
_ENV_FILES = tuple(
    _CONFIG_PATH.parents[i] / ".env"
    for i in (4, 2)
    if i < len(_CONFIG_PATH.parents)
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES)

    # Blueprint-aligned settings
    app_env: str = Field(default="dev", alias="APP_ENV")
    api_secret: str = Field(default="change_me", alias="API_SECRET")
    # Session tokens (HS256 JWT) are signed with api_secret. Set a strong
    # API_SECRET in production -- anyone with it can mint valid tokens.
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expires_minutes: int = Field(
        default=60 * 24 * 7, alias="ACCESS_TOKEN_EXPIRES_MINUTES"  # 7 days
    )
    # Fernet key (urlsafe base64, 32 bytes) for encrypting provider OAuth tokens
    # at rest. Generate with: Fernet.generate_key(). Required in production (see
    # the startup validator); dev falls back to a key derived from api_secret so
    # it can run with zero extra config.
    token_encryption_key: str | None = Field(default=None, alias="TOKEN_ENCRYPTION_KEY")
    database_url: str = Field(default="postgresql+psycopg://user:pass@localhost:5432/ai_mailbox", alias="DATABASE_URL")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    # Email classifier backend: "local" (fine-tuned encoder in models/), "gemini"
    # (LLM), "heuristic" (keyword rules), or "auto" (local, else gemini, else
    # heuristic). Local/gemini both fall back gracefully if unavailable.
    classifier_backend: str = Field(default="local", alias="CLASSIFIER_BACKEND")
    classifier_model_path: str = Field(default="models/email-classifier", alias="CLASSIFIER_MODEL_PATH")
    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str | None = Field(default=None, alias="GOOGLE_REDIRECT_URI")
    # Comma-separated list of browser origins allowed to call the API (CORS).
    # Defaults cover the common local frontend dev servers (Next.js, Vite).
    # Set the real frontend origin(s) in production -- never use "*" here while
    # credentialed bearer requests are in play.
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:5173",
        alias="CORS_ORIGINS",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ORIGINS into a clean list, dropping blanks."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        """True unless APP_ENV names a known dev/test environment."""
        return self.app_env.strip().lower() not in _DEV_ENVS

    @model_validator(mode="after")
    def _require_secure_production_config(self) -> "Settings":
        """Refuse to start with insecure defaults in production.

        Dev keeps the convenient scaffold defaults; anything that isn't a known
        dev env must supply real secrets. Collect every problem so the operator
        sees them all at once instead of fixing one and hitting the next.
        """
        # A non-empty encryption key must be a usable Fernet key in any
        # environment, so a typo fails loudly at boot instead of at first use.
        # Blank is deliberately allowed here: in dev it means "derive from
        # API_SECRET", and in production the required-keys check below rejects
        # it -- so there's no insecure silent fallback in prod.
        if self.token_encryption_key:
            try:
                Fernet(self.token_encryption_key.encode())
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "TOKEN_ENCRYPTION_KEY is not a valid Fernet key (urlsafe "
                    "base64, 32 bytes); generate one with Fernet.generate_key()"
                ) from exc

        # Same idea for the JWT algorithm: an unsupported value (or "none",
        # which would skip signature checks) should fail at boot in every
        # environment, not as a confusing PyJWT error on first login.
        if self.jwt_algorithm not in _ALLOWED_JWT_ALGORITHMS:
            allowed = ", ".join(sorted(_ALLOWED_JWT_ALGORITHMS))
            raise ValueError(
                f"JWT_ALGORITHM {self.jwt_algorithm!r} is not supported; "
                f"choose one of: {allowed}"
            )

        if not self.is_production:
            return self

        problems: list[str] = []
        if self.api_secret == _PLACEHOLDER_SECRET:
            problems.append("API_SECRET is still the insecure default 'change_me'")
        elif len(self.api_secret.encode()) < _MIN_SECRET_BYTES:
            problems.append(
                f"API_SECRET must be at least {_MIN_SECRET_BYTES} bytes "
                f"(got {len(self.api_secret.encode())})"
            )

        # Google OAuth is the real sign-in path, so its credentials are required.
        for name, value in (
            ("GOOGLE_CLIENT_ID", self.google_client_id),
            ("GOOGLE_CLIENT_SECRET", self.google_client_secret),
            ("GOOGLE_REDIRECT_URI", self.google_redirect_uri),
        ):
            if not value:
                problems.append(f"{name} is required in production")

        # A dedicated token-encryption key is required in production: deriving it
        # from api_secret would tie rotating the JWT secret to re-encrypting
        # every stored provider token. Dev may fall back to the derived key.
        if not self.token_encryption_key:
            problems.append("TOKEN_ENCRYPTION_KEY is required in production")

        # We send credentialed CORS responses, and "*" with credentials is both
        # forbidden by the spec and handled awkwardly by Starlette -- it echoes
        # the literal "*", browsers reject it, and the operator gets a confusing
        # runtime failure instead of a clear one at boot.
        if "*" in self.cors_origins_list:
            problems.append(
                'CORS_ORIGINS cannot be "*" while credentialed requests are '
                "enabled; list the real frontend origin(s)"
            )

        if problems:
            raise ValueError(
                "Insecure or incomplete production configuration (APP_ENV="
                f"{self.app_env!r}):\n  - " + "\n  - ".join(problems)
            )
        return self

    # Back-compat aliases for earlier scaffold usages
    @property
    def APP_NAME(self) -> str:  # pragma: no cover
        return "AI Mailbox API"

    @property
    def ENV(self) -> str:  # pragma: no cover
        return self.app_env

    @property
    def DATABASE_URL(self) -> str:  # pragma: no cover
        return self.database_url

    @property
    def REDIS_URL(self) -> str:  # pragma: no cover
        return self.redis_url

    @property
    def SECRET_KEY(self) -> str:  # pragma: no cover
        return self.api_secret

settings = Settings()
