"""Retire the Bilibili hotspot radar and remove its public entries and jobs."""

import sqlalchemy as sa
from alembic import op

revision = "c02b7f3a914"
down_revision = "b741e065ab20"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            "DELETE FROM model_calls WHERE task_id IN "
            "(SELECT id FROM tasks WHERE kind='shared_radar')"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM retrieval_references WHERE task_id IN "
            "(SELECT id FROM tasks WHERE kind='shared_radar')"
        )
    )
    op.execute(sa.text("DELETE FROM tasks WHERE kind='shared_radar'"))
    op.drop_constraint("ck_task_platform_kind", "tasks", type_="check")
    op.create_check_constraint(
        "ck_task_platform_kind",
        "tasks",
        "user_id IS NOT NULL OR kind IN ('shared_analyze', 'shared_index')",
    )
    op.execute(
        sa.text(
            "DELETE FROM index_states WHERE source_kind='shared' "
            "AND source_id IN (SELECT id FROM shared_entries WHERE kind='hotspot')"
        )
    )
    op.execute(sa.text("DELETE FROM shared_entries WHERE kind='hotspot'"))


def downgrade():
    op.drop_constraint("ck_task_platform_kind", "tasks", type_="check")
    op.create_check_constraint(
        "ck_task_platform_kind",
        "tasks",
        "user_id IS NOT NULL OR kind IN ('shared_radar', 'shared_analyze', 'shared_index')",
    )
