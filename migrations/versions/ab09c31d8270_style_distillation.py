"""Temporary style corpora, saved instructions and version-pinned applications.

Revision ID: ab09c31d8270
Revises: f12a83d604b7
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "ab09c31d8270"
down_revision = "f12a83d604b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("result", JSONB(), nullable=True))
    op.create_table(
        "style_corpora",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("preview", JSONB(), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleaned_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_style_corpora_user_id", "style_corpora", ["user_id"])
    op.create_index("ix_style_corpora_expires_at", "style_corpora", ["expires_at"])
    op.create_table(
        "style_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_style_profiles_user_id", "style_profiles", ["user_id"])
    op.create_table(
        "style_profile_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "profile_id",
            sa.Uuid(),
            sa.ForeignKey("style_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("report", JSONB(), nullable=False),
        sa.Column("source_task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("profile_id", "version"),
    )
    op.create_index(
        "ix_style_profile_versions_profile_id", "style_profile_versions", ["profile_id"]
    )
    op.create_table(
        "style_applications",
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column(
            "version_id",
            sa.Uuid(),
            sa.ForeignKey("style_profile_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_table(
        "project_style_overrides",
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "version_id",
            sa.Uuid(),
            sa.ForeignKey("style_profile_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    for table in (
        "project_style_overrides",
        "style_applications",
        "style_profile_versions",
        "style_profiles",
        "style_corpora",
    ):
        op.drop_table(table)
    op.drop_column("tasks", "result")
