"""Operational tables (Section 4.8).

``recordings`` tracks the recorder lifecycle (temp → Drive → transcribe →
delete temp). ``llm_baselines`` is the multi-LLM commodity-floor panel.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
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
