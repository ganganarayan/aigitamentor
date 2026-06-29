"""Embeddings via OpenAI text-embedding-3-small (1536-dim).

Guarded: raises RuntimeError if the key is missing so the ingestion job can
report a clean failure instead of crashing. Batches inputs in one call.
"""

from __future__ import annotations

from app.config import settings
from app.models.corpus import EMBED_DIM


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    import openai

    client = openai.OpenAI(api_key=settings.openai_api_key)
    resp = client.embeddings.create(model=settings.embedding_model, input=texts)
    vectors = [d.embedding for d in resp.data]
    for v in vectors:
        if len(v) != EMBED_DIM:
            raise RuntimeError(
                f"Embedding dim {len(v)} != expected {EMBED_DIM}; "
                f"check EMBEDDING_MODEL ({settings.embedding_model})."
            )
    return vectors


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
