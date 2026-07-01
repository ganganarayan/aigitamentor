"""usage_counters.conversation_tokens (token metering)

Revision ID: 0004_usage_tokens
Revises: 0003_user_plan_expiry
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_usage_tokens"
down_revision: Union[str, None] = "0003_user_plan_expiry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usage_counters",
        sa.Column("conversation_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("usage_counters", "conversation_tokens")
