"""System A — publishing seeker answers to the crawlable /learn graph (Phase 7).

The public-KB law: only **Seeker-tier**, **published** answers may become public
articles — the free, meant-to-be-cited depth. Paid-depth answers and the pgvector
corpus are never published here. Each article is built for citation: clean Q/A,
canonical URL, and FAQPage + BreadcrumbList JSON-LD.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KbAnswer, PublicKbArticle, Question, Verse

logger = logging.getLogger("app.public_kb")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_VERSE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,3})\b")


def slugify(text: str, max_len: int = 80) -> str:
    base = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return base[:max_len].strip("-") or "answer"


def _unique_slug(db: Session, desired: str, article_id: int | None) -> str:
    slug = desired
    n = 1
    while True:
        clash = db.execute(
            select(PublicKbArticle).where(PublicKbArticle.slug == slug)
        ).scalars().first()
        if clash is None or clash.id == article_id:
            return slug
        n += 1
        slug = f"{desired}-{n}"


def _paragraphs_to_html(text: str) -> str:
    """Escape + wrap plain text into <p> blocks (blank-line separated)."""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    out = []
    for b in blocks:
        b = b.strip()
        if b:
            out.append("<p>" + html.escape(b).replace("\n", "<br>") + "</p>")
    return "\n".join(out)


def _summary_from(text: str, limit: int = 200) -> str:
    flat = re.sub(r"\s+", " ", (text or "").strip())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def _primary_verse(db: Session, answer: KbAnswer) -> Verse | None:
    text = answer.answer_final or ""
    m = _VERSE_RE.search(text)
    if not m:
        return None
    ref = f"{m.group(1)}.{m.group(2)}"
    return db.execute(select(Verse).where(Verse.verse_ref == ref)).scalars().first()


def can_publish(answer: KbAnswer | None) -> tuple[bool, str]:
    """Guard the public-KB law. Returns (ok, reason)."""
    if answer is None:
        return False, "Answer not found."
    if (answer.tier or "seeker").lower() != "seeker":
        return False, "Only Seeker-tier answers may be published publicly."
    if answer.status != "published":
        return False, "Mark the answer as published before promoting it to /learn."
    if not (answer.answer_final or "").strip():
        return False, "The answer has no final text."
    return True, ""


def publish_answer(db: Session, answer_id: int) -> tuple[PublicKbArticle | None, str]:
    """Create or refresh the public article for a seeker answer. (article, error)."""
    answer = db.get(KbAnswer, answer_id)
    ok, reason = can_publish(answer)
    if not ok:
        return None, reason

    question = db.get(Question, answer.question_id) if answer.question_id else None
    q_text = (question.question_text if question else None) or _summary_from(answer.answer_final, 120)

    article = db.execute(
        select(PublicKbArticle).where(PublicKbArticle.source_answer_id == answer.id)
    ).scalars().first()
    if article is None:
        article = PublicKbArticle(source_answer_id=answer.id)
        db.add(article)
        article.slug = _unique_slug(db, slugify(q_text), None)
    # (slug is kept stable across re-publishes so external citations don't break)

    verse = _primary_verse(db, answer)
    base = (settings.app_base_url or "").rstrip("/")
    canonical = f"{base}/learn/{article.slug}"

    article.question = q_text
    article.answer_html = _paragraphs_to_html(answer.answer_final)
    article.summary = _summary_from(answer.answer_final)
    article.primary_verse_id = verse.id if verse else None
    article.meta_title = (q_text[:110] + " · AI Gita Mentor")[:300]
    article.meta_description = _summary_from(answer.answer_final, 155)
    article.canonical_url = canonical
    article.faq_schema_json = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": q_text,
                "acceptedAnswer": {"@type": "Answer", "text": _summary_from(answer.answer_final, 900)},
            }
        ],
    }
    article.breadcrumb_json = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Learn", "item": base + "/learn"},
            {"@type": "ListItem", "position": 3, "name": q_text[:80], "item": canonical},
        ],
    }
    article.published = True
    article.published_at = article.published_at or dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(article)
    return article, ""


def unpublish(db: Session, article_id: int) -> None:
    article = db.get(PublicKbArticle, article_id)
    if article is not None:
        article.published = False
        db.commit()


def published_articles(db: Session, limit: int = 500) -> list[PublicKbArticle]:
    return list(
        db.execute(
            select(PublicKbArticle)
            .where(PublicKbArticle.published.is_(True))
            .order_by(PublicKbArticle.published_at.desc().nullslast())
            .limit(limit)
        ).scalars()
    )


def get_published(db: Session, slug: str) -> PublicKbArticle | None:
    return db.execute(
        select(PublicKbArticle).where(
            PublicKbArticle.slug == slug, PublicKbArticle.published.is_(True)
        )
    ).scalars().first()
