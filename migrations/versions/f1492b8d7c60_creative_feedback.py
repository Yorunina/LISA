"""Separate per-work creative feedback from QQ activity."""

import sqlalchemy as sa
from alembic import op

revision = "f1492b8d7c60"
down_revision = "e0386c91a247"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("last_creative_feedback_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "last_creative_feedback_at")
