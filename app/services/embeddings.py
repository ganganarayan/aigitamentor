"""Embeddings via OpenAI text-embedding-3-small (1536-dim).

Guarded: raises RuntimeError if the key is missing so the ingestion job can
report a clean failure instead of crashing. Batches inputs in one call.
"""

from __future__ import annotations

from app.models.corpus import EMBED_DIM


def embed_texts(texts: list[str], api_key: str | None, model: str) -> list[list[float]]:
    if not texts:
        return []
    if not api_key:
        raise RuntimeError("OpenAI API key not configured")
    import openai

    client = openai.OpenAI(api_key=api_key)
    resp = client.embeddings.create(model=model, input=texts)
    vectors = [d.embedding for d in resp.data]
    for v in vectors:
        if len(v) != EMBED_DIM:
            raise RuntimeError(
                f"Embedding dim {len(v)} != expected {EMBED_DIM}; check the embedding model ({model})."
            )
    return vectors


def embed_query(text: str, api_key: str | None, model: str) -> list[float]:
    return embed_texts([text], api_key, model)[0]
