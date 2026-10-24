"""Authorized, passive group records and revocable temporary source snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "d024ab61c370"
down_revision = "c91874a2e603"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts", sa.Column("is_admin", sa.Boolean(), server_default="false", nullable=False)
    )
    op.create_table(
        "collected_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("bot_id", sa.String(32), nullable=False),
        sa.Column("qq_group_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_ordinal", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("bot_id", "qq_group_id"),
    )
    op.create_table(
        "group_grants",
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("collected_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "group_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("collected_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_id", sa.String(48), nullable=False),
        sa.Column("qq_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("group_id", "message_id"),
    )
    op.create_index("ix_group_messages_group_time", "group_messages", ["group_id", "sent_at", "id"])
    op.create_index(
        "ix_group_messages_member_time", "group_messages", ["group_id", "qq_id", "sent_at", "id"]
    )
    op.add_column("style_corpora", sa.Column("source_group_id", sa.Uuid(), nullable=True))
    op.add_column("style_corpora", sa.Column("source_revision", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_style_corpora_source_group_id",
        "style_corpora",
        "collected_groups",
        ["source_group_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_style_corpora_source_group_id", "style_corpora", ["source_group_id"])


def downgrade() -> None:
    op.drop_index("ix_style_corpora_source_group_id", table_name="style_corpora")
    op.drop_constraint("fk_style_corpora_source_group_id", "style_corpora", type_="foreignkey")
    op.drop_column("style_corpora", "source_revision")
    op.drop_column("style_corpora", "source_group_id")
    op.drop_table("group_messages")
    op.drop_table("group_grants")
    op.drop_table("collected_groups")
    op.drop_column("accounts", "is_admin")
