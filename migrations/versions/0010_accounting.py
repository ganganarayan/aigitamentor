"""accounting: llm_baselines token/cost columns + expenses table

Idempotent (0001 create_all may pre-make these on a fresh DB — see deploy notes).

Revision ID: 0010_accounting
Revises: 0009_public_kb
Create Date: 2026-07-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010_accounting"
down_revision: Union[str, None] = "0009_public_kb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())

    base_cols = {c["name"] for c in insp.get_columns("llm_baselines")}
    if "tokens_in" not in base_cols:
        op.add_column("llm_baselines", sa.Column("tokens_in", sa.Integer(), nullable=True))
    if "tokens_out" not in base_cols:
        op.add_column("llm_baselines", sa.Column("tokens_out", sa.Integer(), nullable=True))
    if "cost_inr" not in base_cols:
        op.add_column("llm_baselines", sa.Column("cost_inr", sa.Float(), nullable=True))

    if not insp.has_table("expenses"):
        op.create_table(
            "expenses",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("category", sa.String(length=40), nullable=False, server_default="manual"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("amount", sa.Float(), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(length=8), nullable=False, server_default="INR"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_expenses_category", "expenses", ["category"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("expenses"):
        op.drop_table("expenses")
    cols = {c["name"] for c in insp.get_columns("llm_baselines")}
    for col in ("cost_inr", "tokens_out", "tokens_in"):
        if col in cols:
            op.drop_column("llm_baselines", col)
