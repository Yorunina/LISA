"""Per-project optional author retrieval settings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d27c893e40b1"
down_revision = "b71a09d62f83"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "projects",
        sa.Column("retrieval_config", postgresql.JSONB(), nullable=False, server_default="{}"),
    )


def downgrade():
    op.drop_column("projects", "retrieval_config")
