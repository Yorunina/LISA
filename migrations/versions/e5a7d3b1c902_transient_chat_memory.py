"""Stop retaining raw chat history and keep agent memory task provenance."""

import sqlalchemy as sa
from alembic import op

revision = "e5a7d3b1c902"
down_revision = "d2a1f3c8e4b6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "memories",
        sa.Column(
            "source_task_id",
            sa.Uuid(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_memories_source_task_id", "memories", ["source_task_id"], unique=False)
    # Existing raw messages are the feature being removed.  Preserve the
    # independently written memory text, but sever all raw-chat provenance
    # before deleting the message archive.
    op.execute(sa.text("UPDATE memories SET source_message_id = NULL"))
    op.execute(sa.text("DELETE FROM chat_messages"))


def downgrade():
    op.execute(sa.text("DELETE FROM memories WHERE source_task_id IS NOT NULL"))
    op.drop_index("ix_memories_source_task_id", table_name="memories")
    op.drop_column("memories", "source_task_id")
