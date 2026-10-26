"""Remove legacy weekly proposal items without a time slot."""

import sqlalchemy as sa
from alembic import op

revision = "d2a1f3c8e4b6"
down_revision = "c7e2a4d19f30"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        """
        UPDATE life_week_plans
        SET items = COALESCE(
                (
                    SELECT jsonb_agg(entry ORDER BY position)
                    FROM jsonb_array_elements(items) WITH ORDINALITY AS value(entry, position)
                    WHERE NULLIF(BTRIM(entry->>'time'), '') IS NOT NULL
                ),
                '[]'::jsonb
            ),
            revision = revision + 1,
            updated_at = now()
        WHERE EXISTS (
            SELECT 1
            FROM jsonb_array_elements(items) AS value(entry)
            WHERE NULLIF(BTRIM(entry->>'time'), '') IS NULL
        )
        """
    ))


def downgrade():
    # Deleted legacy entries cannot be reconstructed.
    pass
