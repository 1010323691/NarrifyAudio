"""Restore the one-to-one Project and Workspace relationship for legacy rows.

Revision ID: 0013_project_workspace_pairs
Revises: 0012_task_retry_eligibility
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_project_workspace_pairs"
down_revision = "0012_task_retry_eligibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    projects = sa.Table("projects", metadata, autoload_with=bind)
    workspaces = sa.Table("workspaces", metadata, autoload_with=bind)
    users = sa.Table("users", metadata, autoload_with=bind)
    usernames = dict(bind.execute(sa.select(users.c.id, users.c.username)).all())
    project_rows = {row["id"]: row for row in bind.execute(sa.select(projects)).mappings()}
    workspace_rows = {row["id"]: row for row in bind.execute(sa.select(workspaces)).mappings()}

    for project_id, project in project_rows.items():
        workspace = workspace_rows.get(project_id)
        if workspace is None:
            username = usernames.get(project["owner_id"])
            if username is None:
                raise RuntimeError(f"Project owner missing: {project_id}")
            bind.execute(workspaces.insert().values(
                id=project_id, owner_id=project["owner_id"], name=project["name"],
                directory_key=f"{username}/{project_id}",
                created_at=project["created_at"], updated_at=project["updated_at"],
                deleted_at=project["deleted_at"],
            ))
        elif workspace["owner_id"] != project["owner_id"]:
            raise RuntimeError(f"Project/Workspace owner mismatch: {project_id}")

    for workspace_id, workspace in workspace_rows.items():
        if workspace_id not in project_rows:
            bind.execute(projects.insert().values(
                id=workspace_id, owner_id=workspace["owner_id"], name=workspace["name"],
                description="由历史工作空间恢复",
                created_at=workspace["created_at"], updated_at=workspace["updated_at"],
                deleted_at=workspace["deleted_at"],
            ))
        else:
            project = project_rows[workspace_id]
            deleted_at = project["deleted_at"] or workspace["deleted_at"]
            if deleted_at is not None:
                if project["deleted_at"] is None:
                    bind.execute(projects.update().where(projects.c.id == workspace_id).values(deleted_at=deleted_at))
                if workspace["deleted_at"] is None:
                    bind.execute(workspaces.update().where(workspaces.c.id == workspace_id).values(deleted_at=deleted_at))


def downgrade() -> None:
    # Backfilled rows are user data; reverting code must not delete them.
    pass
