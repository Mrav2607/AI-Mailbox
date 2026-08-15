from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import EncryptedText


class UserLlmCredential(Base):
    """
    One BYOK LLM credential per user, used for action extraction. Presets pin
    their base_url server-side; only "custom" accepts a caller-supplied one,
    and only through the destination policy in services/nlp/providers.py.
    """

    __tablename__ = "user_llm_credential"
    __table_args__ = (
        # Exactly one credential per user -- multi-credential profiles are deferred.
        UniqueConstraint("user_id", name="uq_llm_credential_user"),
        CheckConstraint(
            "provider IN ('openai', 'gemini', 'openrouter', 'groq', 'mistral', 'custom')",
            name="ck_llm_credential_provider",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    # Always stored resolved and normalized -- presets pin it, only "custom"
    # accepts a caller value, and only through the destination policy.
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # Stamped by a successful /test only, via the revision-conditioned UPDATE.
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Incremented on every PUT; the /test success stamp conditions on it so a
    # credential replaced mid-test can never be marked verified by the old
    # credential's test call.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Separate from having a credential at all: extraction runs on a settled
    # verdict subset, classification runs on every ingested message, so a
    # user who opts a key into one has NOT agreed to pay for the other.
    classification_byok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # A true opt-in (default false, plan: 2026-08-14-llm-failure-visibility
    # phase 2, D-A): only meaningful when classification_byok is also set --
    # whether a failed BYOK classification call should serve the local
    # encoder instead of leaving the message unclassified. Writable
    # independently of classification_byok; it's simply inert until that's
    # also true (see llm_settings.py).
    classification_fallback_local: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
