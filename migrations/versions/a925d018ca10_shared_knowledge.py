"""Deployment-wide knowledge, contributions and platform task ownership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "a925d018ca10"
down_revision = "d8193f6a20b4"
branch_labels = None
depends_on = None


def account(name):
    return sa.Column(name, sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="SET NULL"))


def upgrade():
    op.create_table(
        "shared_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("digest", sa.String(64), unique=True, nullable=False),
        sa.Column("mime", sa.String(32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        account("uploader_id"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "shared_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("aliases", pg.JSONB(), server_default="[]", nullable=False),
        sa.Column("tags", pg.JSONB(), server_default="[]", nullable=False),
        sa.Column("source_url", sa.Text(), server_default="", nullable=False),
        sa.Column("source_note", sa.Text(), server_default="", nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("origin_key", sa.String(160), unique=True),
        account("contributor_id"),
        account("editor_id"),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("shared_assets.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("human_edited", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("allow_chat", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("allow_writing", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("source_time", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_shared_entries_contributor_id", "shared_entries", ["contributor_id"])
    op.create_table(
        "shared_revisions",
        sa.Column(
            "entry_id",
            sa.Uuid(),
            sa.ForeignKey("shared_entries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        account("editor_id"),
        sa.Column("data", pg.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "shared_preferences",
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("meme_mode", sa.String(16), server_default="normal", nullable=False),
    )
    op.create_table(
        "shared_mutes",
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "entry_id",
            sa.Uuid(),
            sa.ForeignKey("shared_entries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    for table in ("tasks", "model_calls", "index_states"):
        op.alter_column(table, "user_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint(
        "ck_task_platform_kind",
        "tasks",
        "user_id IS NOT NULL OR kind IN ('shared_radar', 'shared_analyze', 'shared_index')",
    )
    op.create_check_constraint(
        "ck_index_public", "index_states", "user_id IS NOT NULL OR source_kind = 'shared'"
    )
    op.add_column(
        "outbound_messages",
        sa.Column("meme_id", sa.Uuid(), sa.ForeignKey("shared_entries.id", ondelete="SET NULL")),
    )
    op.add_column("outbound_messages", sa.Column("meme_revision", sa.Integer()))


def downgrade():
    op.drop_column("outbound_messages", "meme_revision")
    op.drop_column("outbound_messages", "meme_id")
    op.drop_constraint("ck_index_public", "index_states")
    op.drop_constraint("ck_task_platform_kind", "tasks")
    # Public derived data and jobs cannot be reinterpreted as somebody's private records.
    op.execute("DELETE FROM model_calls WHERE user_id IS NULL")
    op.execute("DELETE FROM index_states WHERE user_id IS NULL")
    op.execute("DELETE FROM tasks WHERE user_id IS NULL")
    for table in ("index_states", "model_calls", "tasks"):
        op.alter_column(table, "user_id", existing_type=sa.Uuid(), nullable=False)
    for table in (
        "shared_mutes",
        "shared_preferences",
        "shared_revisions",
        "shared_entries",
        "shared_assets",
    ):
        op.drop_table(table)
