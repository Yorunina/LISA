"""Current-source vector slices and metadata-only retrieval references."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "f47c1d9b50a6"
down_revision = "e2b751d413c9"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "index_states",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("space", sa.String(64), nullable=False),
        sa.Column("indexed_until", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("complete", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.UniqueConstraint("source_kind", "source_id"),
    )
    op.create_index("ix_index_states_user_id", "index_states", ["user_id"])
    op.create_table(
        "index_chunks",
        sa.Column(
            "state_id",
            sa.Uuid(),
            sa.ForeignKey("index_states.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chunk_index", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("space", sa.String(64), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("end", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
    )
    op.create_index("ix_index_chunks_space", "index_chunks", ["space"])
    op.create_table(
        "retrieval_references",
        sa.Column(
            "task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("space", sa.String(64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("end", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("retrieval_references")
    op.drop_table("index_chunks")
    op.drop_table("index_states")
