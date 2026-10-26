"""Store private weekly virtual-life proposals separately from life memories."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c7e2a4d19f30"
down_revision = "b11a6d2e91f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "life_week_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "week_start"),
    )
    op.create_index("ix_life_week_plans_user_id", "life_week_plans", ["user_id"])


def downgrade():
    op.drop_index("ix_life_week_plans_user_id", table_name="life_week_plans")
    op.drop_table("life_week_plans")
