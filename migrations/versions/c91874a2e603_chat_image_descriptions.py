"""Transient QQ image locations and text-only visual observations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c91874a2e603"
down_revision = "ab09c31d8270"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_messages", sa.Column("image_urls", JSONB, nullable=False, server_default="[]")
    )
    op.add_column(
        "chat_messages",
        sa.Column("image_description", sa.Text(), nullable=False, server_default=""),
    )


def downgrade():
    op.drop_column("chat_messages", "image_description")
    op.drop_column("chat_messages", "image_urls")
