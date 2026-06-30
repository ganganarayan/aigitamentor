"""Ingestion pipeline: published kb_answers → chunk → embed → kb_chunks.

Idempotent and re-runnable: ingesting an answer first deletes its existing
chunks, then inserts fresh ones with full attribution (Section 4.3 / 6 step 8).
``min_tier`` is derived from the answer's editorial tier — that is the gate.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import KbAnswer, KbChunk, KbSource, Question, Verse
from app.models.corpus import tier_level
from app.services import ai_settings
from app.services.embeddings import embed_texts

logger = logging.getLogger("app.ingestion")

_CHUNK_SIZE = 1200
_CHUNK_OVERLAP = 150


def chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Paragraph-aware packing into ~size-char chunks with overlap."""
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}".strip()
        else:
            if buf:
                chunks.append(buf)
            # Carry a tail of the previous chunk for context continuity.
            tail = buf[-overlap:] if buf else ""
            buf = (tail + "\n\n" + para).strip() if tail else para
            # A single oversized paragraph is hard-split.
            while len(buf) > size:
                chunks.append(buf[:size])
                buf = buf[size - overlap:]
    if buf:
        chunks.append(buf)
    return chunks


def _attribution(answer: KbAnswer, source: KbSource | None, question: Question | None, verse_ref: str | None) -> dict:
    return {
        "origin": source.type if source else "manual",
        "source_id": answer.source_id,
        "gdrive_file_id": source.gdrive_file_id if source else None,
        "question_id": answer.question_id,
        "verse_ref": verse_ref,
        "tier": answer.tier,
        "version": answer.version,
        "uploaded_by": source.uploaded_by if source else None,
    }


def _verse_ref_for(db: Session, question: Question | None) -> str | None:
    if question is None or not question.gita_reference:
        return None
    ref = question.gita_reference.split("-")[0].strip()
    verse = db.execute(select(Verse).where(Verse.verse_ref == ref)).scalar_one_or_none()
    return verse.verse_ref if verse else ref


def ingest_answer(db: Session, answer: KbAnswer) -> int:
    """(Re)ingest a single published answer. Returns the number of chunks written."""
    text = answer.answer_final or answer.transcript_edited or answer.transcript_raw or ""
    pieces = chunk_text(text)

    # Idempotent: clear prior chunks for this answer before re-inserting.
    db.execute(delete(KbChunk).where(KbChunk.answer_id == answer.id))

    if not pieces:
        db.commit()
        return 0

    source = db.get(KbSource, answer.source_id) if answer.source_id else None
    question = db.get(Question, answer.question_id) if answer.question_id else None
    verse_ref = _verse_ref_for(db, question)
    min_tier = tier_level(answer.tier)
    attribution = _attribution(answer, source, question, verse_ref)

    cfg = ai_settings.resolved(db)
    vectors = embed_texts(pieces, cfg.key_for("openai"), cfg.embedding_model)  # raises if no key
    for idx, (piece, vector) in enumerate(zip(pieces, vectors)):
        db.add(
            KbChunk(
                answer_id=answer.id,
                source_id=answer.source_id,
                chunk_index=idx,
                chunk_text=piece,
                embedding=vector,
                min_tier=min_tier,
                attribution=attribution,
            )
        )
    db.commit()
    return len(pieces)


def ingest_all_published(db: Session) -> dict:
    """Ingest every published answer. Returns a summary."""
    answers = list(
        db.execute(select(KbAnswer).where(KbAnswer.status == "published")).scalars()
    )
    summary = {"answers": 0, "chunks": 0, "errors": 0}
    for answer in answers:
        try:
            summary["chunks"] += ingest_answer(db, answer)
            summary["answers"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad answer shouldn't halt the batch
            logger.warning("Ingestion failed for answer %s: %s", answer.id, exc)
            db.rollback()
            summary["errors"] += 1
    return summary


def chunk_count_for(db: Session, answer_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(KbChunk).where(KbChunk.answer_id == answer_id)
    ).scalar_one()


# --- background-task entry points (own session) -----------------------------

def ingest_answer_by_id(answer_id: int) -> None:
    from app.db import SessionLocal

    if SessionLocal is None:
        return
    with SessionLocal() as db:
        answer = db.get(KbAnswer, answer_id)
        if answer is not None:
            try:
                ingest_answer(db, answer)
            except Exception:  # noqa: BLE001
                logger.exception("Background ingest failed for answer %s", answer_id)
                db.rollback()


def ingest_all_in_background() -> None:
    from app.db import SessionLocal

    if SessionLocal is None:
        return
    with SessionLocal() as db:
        ingest_all_published(db)
