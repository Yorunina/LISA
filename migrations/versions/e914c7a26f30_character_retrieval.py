"""Character visible scene and optional private knowledge recall.

Revision ID: e914c7a26f30
Revises: a8f40e71b93d
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e914c7a26f30"
down_revision = "a8f40e71b93d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "characters", sa.Column("current_scene", sa.Text(), server_default="", nullable=False)
    )
    op.add_column(
        "characters",
        sa.Column("retrieval_config", postgresql.JSONB(), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("characters", "retrieval_config")
    op.drop_column("characters", "current_scene")
