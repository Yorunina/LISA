"""Persist model-selected QQ delivery intervals."""

import sqlalchemy as sa
from alembic import op

revision = "b11a6d2e91f7"
down_revision = "f4d21a8b7c90"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "outbound_messages",
        sa.Column("interval_seconds", sa.Float(), server_default="1", nullable=False),
    )


def downgrade():
    op.drop_column("outbound_messages", "interval_seconds")
