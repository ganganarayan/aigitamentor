"""init schema + pgvector

Enables the pgvector extension BEFORE any table with an embedding column, then
creates the full normalized schema from the ORM metadata (all FK constraints
included), then builds the gated-retrieval indexes:

  * kb_chunks_embedding_idx — HNSW + cosine (vector_cosine_ops, the `<=>`
    operator), with pgvector's default build params (m=16, ef_construction=64).
    OpenAI text-embedding-3-small vectors are normalized, so cosine is correct.
  * kb_chunks_min_tier_idx  — the tier-gate filter index. Retrieval runs
    `WHERE min_tier <= :user_tier_level`, so the paywall is enforced in the DB.

Re-runnable: `CREATE EXTENSION IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`
won't error if the extension was enabled by hand in Railway earlier.

Fallback to recognize: if this reports `extension "vector" is not available`,
the Postgres image lacks pgvector — switch the service to `pgvector/pgvector:pg16`.

Revision ID: 0001_init
Revises:
Create Date: 2026-06-29
"""
from typing import Sequence, Union

from alembic import op

from app.models import Base

# revision identifiers, used by Alembic.
revision: str = "0001_init"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) pgvector MUST exist before any vector(...) column is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2) All tables from the ORM metadata, in FK-dependency order. This includes
    #    kb_chunks (embedding vector(1536) NOT NULL, min_tier smallint, attribution
    #    jsonb) and its answer_id->kb_answers / source_id->kb_sources constraints.
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    # 3) Vector index: HNSW + cosine, pgvector defaults (correct for this scale).
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx "
        "ON kb_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # 4) Tier-gate index: retrieval filters on min_tier <= the user's tier level.
    op.execute("CREATE INDEX IF NOT EXISTS kb_chunks_min_tier_idx ON kb_chunks (min_tier)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS kb_chunks_min_tier_idx")
    op.execute("DROP INDEX IF EXISTS kb_chunks_embedding_idx")
    Base.metadata.drop_all(bind=op.get_bind())
    # The vector extension is left in place — other objects may depend on it.
