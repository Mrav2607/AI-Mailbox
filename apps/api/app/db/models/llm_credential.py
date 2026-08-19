from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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
    Named BYOK LLM credentials, up to five per user, of which at most one is
    active (`uq_llm_credential_user_active` below). Every resolver in
    services/nlp/providers.py -- extraction, classification -- reads only
    the active row; classification/reply-drafting opt-ins travel with
    whichever credential is active (plan: 2026-08-19-multi-credential-llm-
    profiles). Presets pin their base_url server-side; only "custom" accepts
    a caller-supplied one, and only through the destination policy in
    services/nlp/providers.py.
    """

    __tablename__ = "user_llm_credential"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_llm_credential_user_name"),
        # The <=1-active half of the invariant (D2 in the plan above): "at
        # most one active row per user," which a plain unique constraint
        # can't express since it would also forbid multiple INACTIVE rows.
        # Declared here (not just in the migration) so `alembic check` sees
        # it on both sides -- MIGRATION_ONLY_INDEXES in alembic/env.py is the
        # documented fallback if this comparator round-trip ever regresses.
        Index(
            "uq_llm_credential_user_active",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
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
    # Trimmed, 1..60 chars, validated in-route (schemas/llm_settings.py's
    # PUT-has-no-request-schema rationale applies here too -- the new create
    # route validates by hand for the same "never echo a rejected value"
    # discipline). Migration backfill named every pre-existing row "default".
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # At most one true per user, enforced by uq_llm_credential_user_active
    # above -- see providers.py's resolvers for what a false/no-active-row
    # state means at read time.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
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
