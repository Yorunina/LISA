"""QQ binding, recovery, custom persona and fixed service events."""

import sqlalchemy as sa
from alembic import op

revision = "0002_onboarding"
down_revision = "0001_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "qq_bindings",
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("qq_id", sa.String(32), nullable=False, unique=True),
    )
    op.create_table(
        "service_codes",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("qq_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_service_codes_qq_id", "service_codes", ["qq_id"])
    op.create_table(
        "personas",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "persona_versions",
        sa.Column(
            "persona_id",
            sa.Uuid(),
            sa.ForeignKey("personas.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "service_events",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("bot_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(48), nullable=False),
        sa.Column("qq_id", sa.String(32), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.UniqueConstraint("bot_id", "message_id"),
    )
    op.create_index("ix_service_events_qq_id", "service_events", ["qq_id"])
    op.create_table(
        "guide_notices",
        sa.Column("qq_id", sa.String(32), primary_key=True),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    for name in [
        "guide_notices",
        "service_events",
        "persona_versions",
        "personas",
        "service_codes",
        "qq_bindings",
    ]:
        op.drop_table(name)
