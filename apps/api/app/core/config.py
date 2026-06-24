from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


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
    database_url: str = Field(default="postgresql+psycopg://user:pass@localhost:5432/ai_mailbox", alias="DATABASE_URL")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
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
