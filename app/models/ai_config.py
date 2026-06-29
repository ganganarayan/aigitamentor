"""Versioned AI configuration (Section 4.7).

Never overwrite a config — insert a new version and flip ``active``. The
system prompt lives here (and is prompt-cached on the Claude call).
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PkMixin, TimestampMixin


class AiConfig(Base, PkMixin, TimestampMixin):
    __tablename__ = "ai_config"

    version: Mapped[int] = mapped_column(Integer, index=True)
    system_prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(80))
    temperature: Mapped[float] = mapped_column(Float, default=0.7)
    top_k: Mapped[int] = mapped_column(Integer, default=8)
    retrieval_config: Mapped[dict | None] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
