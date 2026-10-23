"""Shared adopted life events and confirmed sharing."""

import sqlalchemy as sa
from alembic import op

revision = "e2b751d413c9"
down_revision = "c326a85ea170"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("memories", sa.Column("source_chapter_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_memories_source_chapter",
        "memories",
        "chapters",
        ["source_chapter_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_memories_source_chapter_id", "memories", ["source_chapter_id"])
    op.add_column("memories", sa.Column("source_version", sa.Integer(), nullable=True))
    op.add_column("memories", sa.Column("life_kind", sa.String(16), nullable=True))
    op.add_column("memories", sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memories", sa.Column("shared_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "life_shares",
        sa.Column(
            "memory_id",
            sa.Uuid(),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("memory_revision", sa.Integer(), nullable=False),
    )
    op.create_index("ix_life_shares_task_id", "life_shares", ["task_id"])


def downgrade():
    op.drop_table("life_shares")
    for column in ("shared_at", "occurred_at", "life_kind", "source_version"):
        op.drop_column("memories", column)
    op.drop_index("ix_memories_source_chapter_id", table_name="memories")
    op.drop_constraint("fk_memories_source_chapter", "memories", type_="foreignkey")
    op.drop_column("memories", "source_chapter_id")
