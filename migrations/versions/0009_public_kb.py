"""public knowledge graph: public_kb_articles (System A, crawlable /learn)

Idempotent: migration 0001 builds the schema via ``Base.metadata.create_all``,
which already creates ``public_kb_articles`` (its model has existed since day
one). So on every real database the table is already present — we only create it
if somehow missing, and otherwise just advance the version.

Revision ID: 0009_public_kb
Revises: 0008_escalation
Create Date: 2026-07-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009_public_kb"
down_revision: Union[str, None] = "0008_escalation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("public_kb_articles"):
        return  # already built by 0001's create_all — nothing to do

    op.create_table(
        "public_kb_articles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("answer_html", sa.Text(), nullable=True),
        sa.Column("primary_verse_id", sa.BigInteger(), nullable=True),
        sa.Column("related_concept_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("related_article_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("meta_title", sa.String(length=300), nullable=True),
        sa.Column("meta_description", sa.String(length=500), nullable=True),
        sa.Column("faq_schema_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("breadcrumb_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("canonical_url", sa.String(length=500), nullable=True),
        sa.Column("source_answer_id", sa.BigInteger(), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["primary_verse_id"], ["verses.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_answer_id"], ["kb_answers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_public_kb_articles_slug", "public_kb_articles", ["slug"], unique=True)
    op.create_index("ix_public_kb_articles_published", "public_kb_articles", ["published"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("public_kb_articles"):
        op.drop_table("public_kb_articles")
