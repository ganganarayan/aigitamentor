"""Tier-gated vector retrieval over kb_chunks.

The single canonical query the whole product routes through. The gate
(`min_tier <= :tier`) is enforced in SQL — a Seeker (tier level 0) can never
retrieve abhyasi/sadhaka chunks regardless of what the prompt asks for.

Distance is cosine (`<=>`), matching the HNSW `vector_cosine_ops` index built
in migration 0001. OpenAI text-embedding-3-small vectors are normalized, so
cosine distance is the right metric.
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.corpus import tier_level

# $1 = query embedding, $2 = user's tier level, $3 = k  (named binds here).
_SEARCH_SQL = text(
    """
    SELECT id,
           answer_id,
           source_id,
           chunk_text,
           attribution,
           min_tier,
           embedding <=> CAST(:q AS vector) AS distance
    FROM kb_chunks
    WHERE min_tier <= :tier
    ORDER BY embedding <=> CAST(:q AS vector)
    LIMIT :k
    """
)


def _to_vector_literal(embedding: Sequence[float]) -> str:
    """Render a Python float sequence as a pgvector literal: ``[0.1,0.2,...]``."""
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


def search_chunks(
    db: Session,
    query_embedding: Sequence[float],
    user_tier: str | int = "seeker",
    k: int = 8,
) -> list[dict]:
    """Return up to ``k`` nearest gated chunks for the user's tier.

    ``user_tier`` accepts a tier string ("seeker"/"abhyasi"/"sadhaka") or an
    already-resolved integer level.
    """
    level = user_tier if isinstance(user_tier, int) else tier_level(user_tier)
    rows = db.execute(
        _SEARCH_SQL,
        {"q": _to_vector_literal(query_embedding), "tier": level, "k": k},
    ).mappings()
    return [dict(row) for row in rows]
