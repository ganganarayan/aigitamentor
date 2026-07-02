"""Operational tables (Section 4.8).

``recordings`` tracks the recorder lifecycle (temp → Drive → transcribe →
delete temp). ``llm_baselines`` is the multi-LLM commodity-floor panel.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PkMixin, TimestampMixin


class Recording(Base, PkMixin, TimestampMixin):
    __tablename__ = "recordings"

    question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id", ondelete="SET NULL"), index=True)
    admin_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    railway_temp_path: Mapped[str | None] = mapped_column(Text)
    gdrive_file_id: Mapped[str | None] = mapped_column(String(120))
    gdrive_url: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    transcript_text: Mapped[str | None] = mapped_column(Text)
    # recording|uploaded_drive|transcribing|transcribed|temp_deleted|failed
    status: Mapped[str] = mapped_column(String(30), default="recording", index=True)
    error_text: Mapped[str | None] = mapped_column(Text)


class LlmBaseline(Base, PkMixin, TimestampMixin):
    __tablename__ = "llm_baselines"

    question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(30))  # claude|openai|gemini|perplexity
    answer_text: Mapped[str | None] = mapped_column(Text)
    # Cost bookkeeping — filled when the response is pulled (Accounting → Expenses).
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_inr: Mapped[float | None] = mapped_column(Float)


class Expense(Base, PkMixin):
    """Manual / other business expenses for the Accounting ledger. LLM costs are
    computed from generations + llm_baselines; this holds everything else."""

    __tablename__ = "expenses"

    category: Mapped[str] = mapped_column(String(40), default="manual", index=True)
    description: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # INR
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
