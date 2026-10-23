"""One bounded, delayed autonomous scene plan per user."""

import sqlalchemy as sa
from alembic import op

revision = "a61b2d7c093e"
down_revision = "f47c1d9b50a6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "activity_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("memory_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
    )
    op.create_index("ix_activity_plans_user_id", "activity_plans", ["user_id"])
    op.create_index("ix_activity_plans_due_at", "activity_plans", ["due_at"])
    op.create_index(
        "uq_activity_plans_pending_user",
        "activity_plans",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade():
    op.drop_table("activity_plans")
