"""Bounded temporary chat context across nearby turns."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "b17a8c102801"
down_revision = "b15e8a102810"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_turns",
        sa.Column(
            "task_id", pg.UUID(), sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", pg.UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("qq_id", sa.String(32), nullable=False),
        sa.Column("memory_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("messages", pg.JSONB(), nullable=False, server_default="[]"),
    )
    op.create_index("ix_chat_turns_user_id", "chat_turns", ["user_id"])
    op.create_index("ix_chat_turns_expires_at", "chat_turns", ["expires_at"])


def downgrade():
    op.drop_table("chat_turns")
