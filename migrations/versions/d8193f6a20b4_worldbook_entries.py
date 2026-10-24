"""Project-owned worldbook entries, preserving the original core setting text."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d8193f6a20b4"
down_revision = "f1492b8d7c60"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "projects",
        sa.Column("worldbook_entries", postgresql.JSONB(), server_default="[]", nullable=False),
    )
    # Existing source offsets stay valid, but entries need a fresh derived index.
    op.execute("UPDATE projects SET index_revision = index_revision + 1")


def downgrade():
    op.drop_column("projects", "worldbook_entries")
    op.execute("UPDATE projects SET index_revision = index_revision + 1")
