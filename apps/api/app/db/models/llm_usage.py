from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class LlmUsageDaily(Base):
    """
    One row per (user, calendar day, stage, provider): an additive counter
    bucket for account-level background LLM usage, not per-key billing (see
    docs/plans/2026-08-02-llm-usage-visibility-plan.md §1). Rows are written
    by `UsageAccumulator`'s upsert, never by a plain INSERT/UPDATE from
    request code -- keeps concurrent flushes from different workers additive
    instead of last-write-wins.
    """

    __tablename__ = "llm_usage_daily"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "usage_date", "stage", "provider", name="uq_llm_usage_daily_key"
        ),
        CheckConstraint(
            "stage IN ('classification', 'extraction')",
            name="ck_llm_usage_daily_stage",
        ),
        CheckConstraint(
            "calls >= 0 AND calls_with_total_tokens >= 0 AND prompt_tokens >= 0"
            " AND completion_tokens >= 0 AND total_tokens >= 0",
            name="ck_llm_usage_daily_nonneg",
        ),
        # Standalone -- the unique index above leads with user_id and can't
        # serve the retention tick's plain usage_date range delete.
        Index("ix_llm_usage_daily_usage_date", "usage_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default="gen_random_uuid()",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE")
    )
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    calls_with_total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
