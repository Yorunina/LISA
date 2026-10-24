"""Remove the retired shared network-term collection and its derived data."""

import sqlalchemy as sa
from alembic import op

revision = "b741e065ab20"
down_revision = "a925d018ca10"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text("""
        UPDATE tasks SET status = 'cancelled', error = 'shared_term_removed', finished_at = now()
        WHERE kind IN ('shared_analyze', 'shared_index') AND status IN ('pending', 'running')
        AND (request->>'entry_id' IN (SELECT id::text FROM shared_entries WHERE kind = 'term')
          OR request->>'source_id' IN (SELECT id::text FROM shared_entries WHERE kind = 'term'))
    """)
    )
    for table in ("retrieval_references", "index_states"):
        op.execute(
            sa.text(f"""
            DELETE FROM {table} WHERE source_kind = 'shared'
            AND source_id IN (SELECT id FROM shared_entries WHERE kind = 'term')
        """)
        )
    # Revisions and per-user mutes cascade; other shared entries and private data stay intact.
    op.execute(sa.text("DELETE FROM shared_entries WHERE kind = 'term'"))


def downgrade():
    # Retiring the feature is an explicit deletion; downgrading cannot recreate its content.
    pass
