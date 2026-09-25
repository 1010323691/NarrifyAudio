"""Make Project the sole user-owned workspace identity.

Revision ID: 0014_project_storage_identity
Revises: 0013_project_workspace_pairs
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_project_storage_identity"
down_revision = "0013_project_workspace_pairs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    projects = sa.Table("projects", metadata, autoload_with=bind)
    workspaces = sa.Table("workspaces", metadata, autoload_with=bind)
    sessions = sa.Table("user_sessions", metadata, autoload_with=bind)
    project_rows = {row.id: row for row in bind.execute(sa.select(projects)).mappings()}
    workspace_rows = {row.id: row for row in bind.execute(sa.select(workspaces)).mappings()}

    # Revision 0013 pairs historical rows. Refuse to discard information if
    # the relationship has since become inconsistent or the storage key is not unique.
    if set(project_rows) != set(workspace_rows):
        raise RuntimeError("Project/Workspace identities are not paired; repair before migration")
    keys: set[str] = set()
    for project_id, project in project_rows.items():
        workspace = workspace_rows[project_id]
        if project["owner_id"] != workspace["owner_id"]:
            raise RuntimeError(f"Project/Workspace owner mismatch: {project_id}")
        key = workspace["directory_key"]
        if not key or key in keys:
            raise RuntimeError(f"Missing or duplicate workspace directory key: {project_id}")
        keys.add(key)

    op.add_column("projects", sa.Column("directory_key", sa.String(length=180), nullable=True))
    op.add_column("projects", sa.Column("last_selected_at", sa.DateTime(timezone=True), nullable=True))
    for project_id, workspace in workspace_rows.items():
        bind.execute(
            sa.text("UPDATE projects SET directory_key = :key, last_selected_at = :selected WHERE id = :id")
            .bindparams(key=workspace["directory_key"], selected=workspace["updated_at"], id=project_id)
        )
    with op.batch_alter_table("projects") as batch:
        batch.alter_column("directory_key", existing_type=sa.String(length=180), nullable=False)
        batch.create_unique_constraint("uq_projects_directory_key", ["directory_key"])

    # Repoint stored session selections before removing their old target table.
    inspector = sa.inspect(bind)
    fk_names = [item.get("name") for item in inspector.get_foreign_keys("user_sessions")
                if set(item.get("constrained_columns") or []) == {"active_workspace_id"}]
    index_names = {item["name"] for item in inspector.get_indexes("user_sessions")}
    for name in fk_names:
        if name:
            op.drop_constraint(name, "user_sessions", type_="foreignkey")
    if "ix_user_sessions_active_workspace_id" in index_names:
        op.drop_index("ix_user_sessions_active_workspace_id", table_name="user_sessions")
    with op.batch_alter_table("user_sessions") as batch:
        batch.alter_column("active_workspace_id", new_column_name="active_project_id",
                           existing_type=sa.String(length=36), existing_nullable=True)
        batch.create_foreign_key("fk_user_sessions_active_project_id", "projects",
                                 ["active_project_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_user_sessions_active_project_id", "user_sessions", ["active_project_id"])
    op.drop_table("workspaces")


def downgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("directory_key", sa.String(length=180), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_id", "name", "deleted_at", name="uq_workspaces_owner_name_deleted"),
    )
    bind = op.get_bind()
    projects = sa.Table("projects", sa.MetaData(), autoload_with=bind)
    for project in bind.execute(sa.select(projects)).mappings():
        bind.execute(sa.text(
            "INSERT INTO workspaces (id, owner_id, name, directory_key, created_at, updated_at, deleted_at) "
            "VALUES (:id, :owner_id, :name, :directory_key, :created_at, :updated_at, :deleted_at)"
        ).bindparams(
            id=project["id"], owner_id=project["owner_id"], name=project["name"],
            directory_key=project["directory_key"], created_at=project["created_at"],
            updated_at=project["last_selected_at"] or project["updated_at"], deleted_at=project["deleted_at"],
        ))
    op.create_index("ix_workspaces_owner_id", "workspaces", ["owner_id"])
    op.create_index("ix_workspaces_deleted_at", "workspaces", ["deleted_at"])
    inspector = sa.inspect(bind)
    fk_names = [item.get("name") for item in inspector.get_foreign_keys("user_sessions")
                if set(item.get("constrained_columns") or []) == {"active_project_id"}]
    index_names = {item["name"] for item in inspector.get_indexes("user_sessions")}
    for name in fk_names:
        if name:
            op.drop_constraint(name, "user_sessions", type_="foreignkey")
    if "ix_user_sessions_active_project_id" in index_names:
        op.drop_index("ix_user_sessions_active_project_id", table_name="user_sessions")
    with op.batch_alter_table("user_sessions") as batch:
        batch.alter_column("active_project_id", new_column_name="active_workspace_id",
                           existing_type=sa.String(length=36), existing_nullable=True)
        batch.create_foreign_key("fk_user_sessions_active_workspace_id", "workspaces",
                                 ["active_workspace_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_user_sessions_active_workspace_id", "user_sessions", ["active_workspace_id"])
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("uq_projects_directory_key", type_="unique")
        batch.drop_column("last_selected_at")
        batch.drop_column("directory_key")
