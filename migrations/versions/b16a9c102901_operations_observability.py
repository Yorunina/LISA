"""Minimal private operations observations and isolated RAG runs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "b16a9c102901"
down_revision = "b17a8c102801"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_heartbeats",
        sa.Column("component", sa.String(32), primary_key=True),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("details", pg.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "retrieval_observations",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column("task_id", pg.UUID(), sa.ForeignKey("tasks.id", ondelete="CASCADE")),
        sa.Column("user_id", pg.UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("metrics", pg.JSONB(), nullable=False, server_default="{}"),
    )
    for column in ("task_id", "user_id", "created_at"):
        op.create_index(f"ix_retrieval_observations_{column}", "retrieval_observations", [column])
    op.create_table(
        "rag_evaluation_runs",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("configuration", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("results", pg.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_rag_evaluation_runs_created_at", "rag_evaluation_runs", ["created_at"])


def downgrade():
    op.drop_table("rag_evaluation_runs")
    op.drop_table("retrieval_observations")
    op.drop_table("runtime_heartbeats")
