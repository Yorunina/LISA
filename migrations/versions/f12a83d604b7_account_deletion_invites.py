"""Keep consumed invitation seats after account deletion.

Revision ID: f12a83d604b7
Revises: e914c7a26f30
"""

import sqlalchemy as sa
from alembic import op

revision = "f12a83d604b7"
down_revision = "e914c7a26f30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invites", sa.Column("consumed", sa.Boolean(), server_default="false", nullable=False)
    )
    op.execute("UPDATE invites SET consumed = true WHERE used_by IS NOT NULL")
    op.drop_constraint("invites_used_by_fkey", "invites", type_="foreignkey")
    op.create_foreign_key(
        "invites_used_by_fkey", "invites", "accounts", ["used_by"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    # Do not turn a deleted account's consumed invitation into a usable code on downgrade.
    op.execute("DELETE FROM invites WHERE consumed AND used_by IS NULL")
    op.drop_constraint("invites_used_by_fkey", "invites", type_="foreignkey")
    op.create_foreign_key("invites_used_by_fkey", "invites", "accounts", ["used_by"], ["id"])
    op.drop_column("invites", "consumed")
