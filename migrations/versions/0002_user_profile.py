"""user profile fields (age, profession, gender, onboarded)

Collected on first contact — the personalization differentiator. Existing users
who have already chatted are marked onboarded so they aren't re-asked.

Revision ID: 0002_user_profile
Revises: 0001_init
Create Date: 2026-06-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_user_profile"
down_revision: Union[str, None] = "0001_init"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("age", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("profession", sa.String(length=160), nullable=True))
    op.add_column("users", sa.Column("gender", sa.String(length=40), nullable=True))
    op.add_column(
        "users",
        sa.Column("onboarded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # Don't re-onboard users who already have a chat history.
    op.execute(
        "UPDATE users SET onboarded = true WHERE id IN ("
        " SELECT DISTINCT c.user_id FROM conversations c"
        " JOIN messages m ON m.conversation_id = c.id WHERE m.role = 'assistant')"
    )


def downgrade() -> None:
    for col in ("onboarded", "gender", "profession", "age"):
        op.drop_column("users", col)
