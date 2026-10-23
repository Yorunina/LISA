"""works_chapters_author_tasks"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "06f4d5f2597a"
down_revision = "bff673deb1a0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("completed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("worldbook", sa.Text(), server_default="", nullable=False),
        sa.Column("constraints", sa.Text(), server_default="", nullable=False),
        sa.Column("characters", sa.Text(), server_default="", nullable=False),
        sa.Column("author_prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("plot_state", sa.Text(), server_default="", nullable=False),
        sa.Column("agreement", sa.Text(), server_default="", nullable=False),
        sa.Column("preferences", sa.Text(), server_default="", nullable=False),
        sa.Column("tentative", sa.Text(), server_default="", nullable=False),
        sa.Column("index_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_projects_user_id"), "projects", ["user_id"], unique=False)
    op.create_index(
        "uq_projects_current_user",
        "projects",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "chapters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "position"),
    )
    op.create_index(op.f("ix_chapters_project_id"), "chapters", ["project_id"], unique=False)
    op.create_table(
        "chapter_versions",
        sa.Column("chapter_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("chapter_id", "version"),
        sa.UniqueConstraint("task_id"),
    )
    op.add_column("tasks", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.add_column("tasks", sa.Column("chapter_id", sa.Uuid(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "tasks", sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column("tasks", sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("retry_of", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_tasks_project_id"), "tasks", ["project_id"], unique=False)
    op.create_foreign_key(
        "fk_tasks_chapter", "tasks", "chapters", ["chapter_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_tasks_project", "tasks", "projects", ["project_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_tasks_retry", "tasks", "tasks", ["retry_of"], ["id"], ondelete="SET NULL"
    )


def downgrade():
    op.drop_constraint("fk_tasks_retry", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_project", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_chapter", "tasks", type_="foreignkey")
    op.drop_index(op.f("ix_tasks_project_id"), table_name="tasks")
    op.drop_column("tasks", "retry_of")
    op.drop_column("tasks", "adopted_at")
    op.drop_column("tasks", "input_snapshot")
    op.drop_column("tasks", "request")
    op.drop_column("tasks", "chapter_id")
    op.drop_column("tasks", "project_id")
    op.drop_table("chapter_versions")
    op.drop_index(op.f("ix_chapters_project_id"), table_name="chapters")
    op.drop_table("chapters")
    op.drop_index(
        "uq_projects_current_user", table_name="projects", postgresql_where=sa.text("is_current")
    )
    op.drop_index(op.f("ix_projects_user_id"), table_name="projects")
    op.drop_table("projects")
