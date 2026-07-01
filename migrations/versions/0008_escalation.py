"""escalation layer: video resources, grants, funnel state + assessment date

Revision ID: 0008_escalation
Revises: 0007_user_phone
Create Date: 2026-07-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_escalation"
down_revision: Union[str, None] = "0007_user_phone"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("assessment_taken_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "video_resources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("topic", sa.String(length=60), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("embed_html", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_video_resources_topic", "video_resources", ["topic"])
    op.create_index("ix_video_resources_active", "video_resources", ["active"])

    op.create_table(
        "resource_grants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("video_resource_id", sa.BigInteger(), nullable=True),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_resource_id"], ["video_resources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_resource_grants_user_id", "resource_grants", ["user_id"])
    op.create_index("ix_resource_grants_token", "resource_grants", ["token"], unique=True)

    op.create_table(
        "escalation_states",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=False, server_default="none"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_escalation_states_conversation_id", "escalation_states", ["conversation_id"], unique=True)
    op.create_index("ix_escalation_states_user_id", "escalation_states", ["user_id"])


def downgrade() -> None:
    op.drop_table("escalation_states")
    op.drop_table("resource_grants")
    op.drop_table("video_resources")
    op.drop_column("users", "assessment_taken_at")
