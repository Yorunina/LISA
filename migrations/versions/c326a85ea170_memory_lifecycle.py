"""Memory lifecycle and retained chat source exclusions."""

import sqlalchemy as sa
from alembic import op

revision = "c326a85ea170"
down_revision = "06f4d5f2597a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "accounts", sa.Column("memory_revision", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column(
        "chat_messages", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "chat_messages",
        sa.Column("context_excluded", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column(
            "source_message_id", sa.Uuid(), sa.ForeignKey("chat_messages.id", ondelete="SET NULL")
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), server_default="", nullable=False),
        sa.Column("allow_chat", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("allow_writing", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    for column in ("user_id", "project_id", "source_message_id"):
        op.create_index(f"ix_memories_{column}", "memories", [column])
    op.create_table(
        "memory_uses",
        sa.Column(
            "memory_id",
            sa.Uuid(),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    op.create_index("ix_memory_uses_task_id", "memory_uses", ["task_id"])


def downgrade():
    op.drop_table("memory_uses")
    op.drop_table("memories")
    op.drop_column("chat_messages", "context_excluded")
    op.drop_column("chat_messages", "deleted_at")
    op.drop_column("accounts", "memory_revision")
