"""memory engine: conversation_summaries + user_patterns

Revision ID: 0005_memory_pattern
Revises: 0004_usage_tokens
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_memory_pattern"
down_revision: Union[str, None] = "0004_usage_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("rolling_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("conversation_id"),
    )
    op.create_index("ix_conversation_summaries_user_id", "conversation_summaries", ["user_id"])

    op.create_table(
        "user_patterns",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("core_knot", sa.Text(), nullable=True),
        sa.Column("signature", sa.String(length=200), nullable=True),
        sa.Column("active_domains", postgresql.JSONB(), nullable=True),
        sa.Column("tried_didnt_work", postgresql.JSONB(), nullable=True),
        sa.Column("stage_by_domain", postgresql.JSONB(), nullable=True),
        sa.Column("trajectory", sa.String(length=20), nullable=True),
        sa.Column("archetype_tag", sa.String(length=60), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_patterns")
    op.drop_index("ix_conversation_summaries_user_id", table_name="conversation_summaries")
    op.drop_table("conversation_summaries")
