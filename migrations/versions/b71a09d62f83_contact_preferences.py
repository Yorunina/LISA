"""Explicit contact preferences, initially disabled for every account."""

import sqlalchemy as sa
from alembic import op

revision = "b71a09d62f83"
down_revision = "a61b2d7c093e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "contact_preferences",
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("timezone", sa.String(128), server_default="Asia/Shanghai", nullable=False),
        sa.Column("quiet_start", sa.Integer(), nullable=True),
        sa.Column("quiet_end", sa.Integer(), nullable=True),
        sa.Column("min_interval_minutes", sa.Integer(), server_default="240", nullable=False),
        sa.Column("max_per_day", sa.Integer(), server_default="2", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade():
    op.drop_table("contact_preferences")
