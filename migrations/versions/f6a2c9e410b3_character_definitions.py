"""Store each character's structured definition without rewriting legacy prompts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f6a2c9e410b3"
down_revision = "e5a7d3b1c902"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "characters",
        sa.Column("definition", postgresql.JSONB(), server_default="{}", nullable=False),
    )


def downgrade():
    op.drop_column("characters", "definition")
