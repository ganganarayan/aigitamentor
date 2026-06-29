"""System B — the gated answer corpus (Section 4.3).

Provenance for everything (kb_sources) → tier-tagged answers (kb_answers) →
embedded, attributed chunks (kb_chunks).

The paywall is enforced at the DATABASE level, not just the prompt:
``kb_chunks.min_tier`` is a smallint (0=seeker, 1=abhyasi, 2=sadhaka). A
Seeker's query carries tier level 0, so ``WHERE min_tier <= 0`` can only ever
return Seeker chunks — recorded Sādhaka depth is literally unreachable for a
free user. See app/services/retrieval.py for the canonical gated query.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pgvector.sqlalchemy import Vector

from app.models.base import Base, PkMixin, TimestampMixin

# OpenAI text-embedding-3-small. Do NOT change without re-embedding the corpus
# and altering the column — the migration's vector index is bound to this dim.
EMBED_DIM = 1536

# Editorial tier string  ->  min_tier gate level used on kb_chunks.
TIER_RANK = {"seeker": 0, "abhyasi": 1, "sadhaka": 2}


def tier_level(tier: str) -> int:
    """Map a tier string to its numeric gate level (defaults to seeker=0)."""
    return TIER_RANK.get((tier or "seeker").lower(), 0)


class KbSource(Base, PkMixin, TimestampMixin):
    __tablename__ = "kb_sources"

    type: Mapped[str] = mapped_column(String(30))  # recording|video_transcript|pdf|url|youtube|manual
    title: Mapped[str] = mapped_column(String(300))
    gdrive_file_id: Mapped[str | None] = mapped_column(String(120))
    audio_url: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, default=1)


class KbAnswer(Base, PkMixin, TimestampMixin):
    __tablename__ = "kb_answers"

    question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id", ondelete="SET NULL"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("kb_sources.id", ondelete="SET NULL"), index=True)
    # Editorial depth tag of the answer (drives kb_chunks.min_tier at ingestion).
    tier: Mapped[str] = mapped_column(String(20), default="seeker", index=True)
    transcript_raw: Mapped[str | None] = mapped_column(Text)
    transcript_edited: Mapped[str | None] = mapped_column(Text)
    answer_final: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft|reviewed|published
    version: Mapped[int] = mapped_column(Integer, default=1)


class KbChunk(Base, PkMixin):
    """Embedded retrieval unit. ``attribution`` carries full provenance (origin
    source, question_id, recording_id, audio_timestamp, verse_ref, version).

    Mirrors the canonical 0001 migration exactly. The HNSW (cosine) index and
    the min_tier index are created in that migration, not here.
    """

    __tablename__ = "kb_chunks"

    answer_id: Mapped[int | None] = mapped_column(ForeignKey("kb_answers.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("kb_sources.id", ondelete="SET NULL"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=False)
    # The paywall gate: 0=seeker, 1=abhyasi, 2=sadhaka.
    min_tier: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    attribution: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
