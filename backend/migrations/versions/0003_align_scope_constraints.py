"""align persisted scope constraints with the platform models

Revision ID: 0003_align_scope_constraints
Revises: 0002_usernames_and_workspace_paths
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_align_scope_constraints"
down_revision = "0002_usernames_paths"
branch_labels = None
depends_on = None


def _project_files_table() -> sa.Table:
    metadata = sa.MetaData()
    table = sa.Table(
        "project_files",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(700), nullable=False, unique=True),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_project_files_project_owner",
        ),
        sa.Index("ix_project_files_project_id", "project_id"),
        sa.Index("ix_project_files_owner_id", "owner_id"),
        sa.Index("ix_project_files_deleted_at", "deleted_at"),
        sa.Index("ix_project_files_scope", "owner_id", "project_id", "deleted_at"),
    )
    return table


def _tasks_table() -> sa.Table:
    metadata = sa.MetaData()
    table = sa.Table(
        "tasks",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("progress", sa.Integer, nullable=False),
        sa.Column("error_code", sa.String(100), nullable=False),
        sa.Column("error_message", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.String(180)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_tasks_project_owner",
        ),
        sa.Index("ix_tasks_owner_id", "owner_id"),
        sa.Index("ix_tasks_project_id", "project_id"),
        sa.Index("ix_tasks_status", "status"),
        sa.Index("ix_tasks_scope_status", "owner_id", "project_id", "status"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_tasks_owner_idempotency"),
        sa.CheckConstraint("progress >= 0 and progress <= 100", name="ck_tasks_progress"),
        sa.CheckConstraint(
            "status in ('pending','queued','running','paused','cancelling','cancelled','succeeded','failed','retrying','timeout')",
            name="ck_tasks_status",
        ),
    )
    return table


def _has_composite_foreign_key(bind, table_name: str, name: str) -> bool:
    for foreign_key in sa.inspect(bind).get_foreign_keys(table_name):
        if (
            foreign_key.get("name") == name
            and foreign_key.get("constrained_columns") == ["project_id", "owner_id"]
            and foreign_key.get("referred_columns") == ["id", "owner_id"]
        ):
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()

    # Older auto-created databases may already have the column, but not its
    # backfill, NOT NULL contract, or the index name emitted by the model.
    user_columns = {column["name"]: column for column in sa.inspect(bind).get_columns("users")}
    if "username" in user_columns:
        op.execute(
            sa.text(
                "UPDATE users SET username = 'user-' || substr(replace(id, '-', ''), 1, 12) "
                "WHERE username IS NULL OR username = ''"
            )
        )
        with op.batch_alter_table("users") as batch:
            batch.alter_column("username", existing_type=sa.String(length=64), nullable=False)

        user_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("users")}
        if "ix_users_username_unique" in user_indexes:
            op.drop_index("ix_users_username_unique", table_name="users")
        if "ix_users_username" not in user_indexes:
            op.create_index("ix_users_username", "users", ["username"], unique=True)

    project_uniques = {constraint["name"] for constraint in sa.inspect(bind).get_unique_constraints("projects")}
    if "uq_projects_id_owner" not in project_uniques:
        with op.batch_alter_table("projects") as batch:
            batch.create_unique_constraint("uq_projects_id_owner", ["id", "owner_id"])

    if not _has_composite_foreign_key(bind, "project_files", "fk_project_files_project_owner"):
        with op.batch_alter_table(
            "project_files", recreate="always", copy_from=_project_files_table()
        ):
            pass

    if not _has_composite_foreign_key(bind, "tasks", "fk_tasks_project_owner"):
        with op.batch_alter_table("tasks", recreate="always", copy_from=_tasks_table()):
            pass


def downgrade() -> None:
    # The 0002 schema already permits these constraints, so retaining them on
    # downgrade avoids weakening tenant isolation for an older application.
    pass
