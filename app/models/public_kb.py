"""System A — public, crawlable knowledge pages (Section 4.4).

Curated: auto-drafted from the graph + commodity baselines, human-approved
before publish. Seeker depth only — never paid-depth content.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PkMixin, TimestampMixin


class PublicKbArticle(Base, PkMixin, TimestampMixin):
    __tablename__ = "public_kb_articles"

    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    question: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    answer_html: Mapped[str | None] = mapped_column(Text)  # commodity/seeker depth only
    primary_verse_id: Mapped[int | None] = mapped_column(ForeignKey("verses.id", ondelete="SET NULL"))
    related_concept_ids: Mapped[dict | None] = mapped_column(JSONB)
    related_article_ids: Mapped[dict | None] = mapped_column(JSONB)
    meta_title: Mapped[str | None] = mapped_column(String(300))
    meta_description: Mapped[str | None] = mapped_column(String(500))
    faq_schema_json: Mapped[dict | None] = mapped_column(JSONB)
    breadcrumb_json: Mapped[dict | None] = mapped_column(JSONB)
    canonical_url: Mapped[str | None] = mapped_column(String(500))
    source_answer_id: Mapped[int | None] = mapped_column(ForeignKey("kb_answers.id", ondelete="SET NULL"))
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
