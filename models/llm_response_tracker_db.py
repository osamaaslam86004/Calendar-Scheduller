# models/llm_response_tracker_db.py

import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, JSON, Float, Integer, Boolean, Index
from sqlalchemy.types import Text
from models.base import Base


class LLMUsageLogDB(Base):
    __tablename__ = "llm_usage_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(index=True, nullable=True)
    provider: Mapped[str] = mapped_column(
        String(50), index=True
    )  # e.g., 'openrouter', 'google', 'local'
    model_name: Mapped[str] = mapped_column(String(100), index=True)

    # Text Payloads
    query_text: Mapped[str] = mapped_column(Text)
    system_instruction: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Token Metrics
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)

    # Cache Status & Costing
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    execution_time_ms: Mapped[float] = mapped_column(Float, default=0.0)

    # Metadata
    raw_response_meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )

    __table_args__ = (
        Index("ix_usage_provider_cache", "provider", "cache_hit"),
        Index("ix_usage_created_provider", "created_at", "provider"),
    )
