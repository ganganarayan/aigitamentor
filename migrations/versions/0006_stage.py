"""progressive direction: stage on kb_answers + kb_chunks

Revision ID: 0006_stage
Revises: 0005_memory_pattern
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_stage"
down_revision: Union[str, None] = "0005_memory_pattern"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("kb_answers", sa.Column("stage", sa.String(length=40), nullable=True))
    op.create_index("ix_kb_answers_stage", "kb_answers", ["stage"])
    op.add_column("kb_chunks", sa.Column("stage", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("kb_chunks", "stage")
    op.drop_index("ix_kb_answers_stage", table_name="kb_answers")
    op.drop_column("kb_answers", "stage")
