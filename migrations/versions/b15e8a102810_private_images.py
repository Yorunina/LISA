"""Private image storage, work slots, and consumption-exempt image calls."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "b15e8a102810"
down_revision = "f6a2c9e410b3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "private_images",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column(
            "user_id", pg.UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("mime", sa.String(32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_private_images_user_id", "private_images", ["user_id"])
    op.create_index("ix_private_images_expires_at", "private_images", ["expires_at"])
    op.create_table(
        "image_preferences",
        sa.Column(
            "user_id", pg.UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "lisa_reference_id", pg.UUID(), sa.ForeignKey("private_images.id", ondelete="SET NULL")
        ),
    )
    for table, columns in (
        ("projects", ["cover_image_id", "image_reference_id"]),
        ("chapters", ["illustration_image_id"]),
        ("outbound_messages", ["private_image_id"]),
    ):
        for name in columns:
            op.add_column(
                table,
                sa.Column(name, pg.UUID(), sa.ForeignKey("private_images.id", ondelete="SET NULL")),
            )
    for table in ("projects", "chapters"):
        op.add_column(
            table, sa.Column("image_revision", sa.Integer(), server_default="0", nullable=False)
        )
    op.add_column(
        "projects",
        sa.Column("image_style", sa.String(24), server_default="watercolor", nullable=False),
    )
    op.add_column(
        "projects", sa.Column("image_notes", sa.Text(), server_default="", nullable=False)
    )
    op.add_column(
        "model_calls",
        sa.Column("budget_exempt", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade():
    op.drop_column("model_calls", "budget_exempt")
    for name in (
        "image_notes",
        "image_style",
        "image_revision",
        "cover_image_id",
        "image_reference_id",
    ):
        op.drop_column("projects", name)
    op.drop_column("chapters", "image_revision")
    op.drop_column("chapters", "illustration_image_id")
    op.drop_column("outbound_messages", "private_image_id")
    op.drop_table("image_preferences")
    op.drop_table("private_images")
