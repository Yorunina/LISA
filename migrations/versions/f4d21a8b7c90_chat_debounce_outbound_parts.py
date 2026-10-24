"""Persist chat debounce deadlines and allow ordered outbound parts."""

import sqlalchemy as sa
from alembic import op

revision = "f4d21a8b7c90"
down_revision = "c02b7f3a914"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("outbound_messages_task_id_key", "outbound_messages", type_="unique")
    op.add_column(
        "outbound_messages",
        sa.Column("sequence", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "outbound_messages",
        sa.Column(
            "chat_message_id",
            sa.Uuid(),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_outbound_messages_task_id", "outbound_messages", ["task_id"], unique=False
    )
    op.create_unique_constraint(
        "uq_outbound_messages_task_sequence", "outbound_messages", ["task_id", "sequence"]
    )


def downgrade():
    # Older versions can represent one outbound row per task only.
    op.execute(
        sa.text(
            "DELETE FROM outbound_messages older USING outbound_messages newer "
            "WHERE older.task_id = newer.task_id AND older.id < newer.id"
        )
    )
    op.drop_constraint("uq_outbound_messages_task_sequence", "outbound_messages", type_="unique")
    op.drop_index("ix_outbound_messages_task_id", table_name="outbound_messages")
    op.drop_column("outbound_messages", "chat_message_id")
    op.drop_column("outbound_messages", "sequence")
    op.create_unique_constraint("outbound_messages_task_id_key", "outbound_messages", ["task_id"])
