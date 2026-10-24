"""Revocable public reading links for saved works."""

import sqlalchemy as sa
from alembic import op

revision = "e0386c91a247"
down_revision = "d024ab61c370"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("share_token", sa.String(43), nullable=True))
    op.create_unique_constraint("uq_projects_share_token", "projects", ["share_token"])


def downgrade() -> None:
    op.drop_constraint("uq_projects_share_token", "projects", type_="unique")
    op.drop_column("projects", "share_token")
