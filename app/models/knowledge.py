"""System-wide knowledge graph (Section 4.1 & 4.2).

The normalized knowledge-object model. The keystone is ``relationships``: a
single table that lets any object reference any other, indexed in both
directions. This powers hybrid retrieval and internal linking.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PkMixin, TimestampMixin


class Concept(Base, PkMixin, TimestampMixin):
    __tablename__ = "concepts"

    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(120), index=True)
    meta: Mapped[dict | None] = mapped_column(JSONB)


class Chapter(Base, PkMixin, TimestampMixin):
    __tablename__ = "chapters"

    number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)

    verses: Mapped[list["Verse"]] = relationship(back_populates="chapter")


class Verse(Base, PkMixin, TimestampMixin):
    """Canonical verse text — the deterministic source of truth for the
    verse-accuracy check. Never transcribed; always pre-filled/curated."""

    __tablename__ = "verses"

    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    verse_ref: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # e.g. "2.47"
    sanskrit: Mapped[str | None] = mapped_column(Text)
    transliteration: Mapped[str | None] = mapped_column(Text)
    translation: Mapped[str | None] = mapped_column(Text)
    plain_explanation: Mapped[str | None] = mapped_column(Text)

    chapter: Mapped["Chapter"] = relationship(back_populates="verses")


class Question(Base, PkMixin, TimestampMixin):
    """Seeded from the 160-Q doc."""

    __tablename__ = "questions"

    q_number: Mapped[int | None] = mapped_column(Integer, index=True)  # 1..160
    domain: Mapped[str | None] = mapped_column(String(120), index=True)
    question_text: Mapped[str] = mapped_column(Text)
    gita_reference: Mapped[str | None] = mapped_column(String(120))
    intent_tags: Mapped[dict | None] = mapped_column(JSONB)
    depth_levels: Mapped[dict | None] = mapped_column(JSONB)


class Topic(Base, PkMixin, TimestampMixin):
    """Broader groupings of concepts for navigation/SEO."""

    __tablename__ = "topics"

    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)


class Relationship(Base):
    """Keystone graph edge — any object → any object.

    Example: verse 2.47 -> relates_to -> {Karma, Leadership, Decision-Making}.
    Indexed both directions for traversal during hybrid retrieval.
    """

    __tablename__ = "relationships"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_type: Mapped[str] = mapped_column(String(40))
    from_id: Mapped[int] = mapped_column(BigInteger)
    to_type: Mapped[str] = mapped_column(String(40))
    to_id: Mapped[int] = mapped_column(BigInteger)
    relation: Mapped[str] = mapped_column(String(60))  # relates_to, answers, cites_verse, belongs_to_topic
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_relationships_from", "from_type", "from_id"),
        Index("ix_relationships_to", "to_type", "to_id"),
        Index("ix_relationships_relation", "relation"),
    )
