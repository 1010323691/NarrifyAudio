"""backfill one durable project for every managed legacy workspace

Revision ID: 0008_workspace_projects
Revises: 0007_quota_ledger_snapshots
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "0008_workspace_projects"
down_revision = "0007_quota_ledger_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    workspace = sa.table(
        "workspaces",
        sa.column("id", sa.String(36)),
        sa.column("owner_id", sa.String(36)),
        sa.column("name", sa.String(160)),
        sa.column("deleted_at", sa.DateTime(timezone=True)),
    )
    project = sa.table(
        "projects",
        sa.column("id", sa.String(36)),
        sa.column("owner_id", sa.String(36)),
        sa.column("name", sa.String(160)),
        sa.column("description", sa.Text()),
        sa.column("deleted_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(
        sa.select(workspace.c.id, workspace.c.owner_id, workspace.c.name, workspace.c.deleted_at)
    ).all()
    now = datetime.now(timezone.utc)
    for row in rows:
        exists = bind.execute(sa.select(project.c.id).where(project.c.id == row.id)).first()
        if exists is not None:
            continue
        bind.execute(
            project.insert().values(
                id=row.id,
                owner_id=row.owner_id,
                name=row.name,
                description="由兼容工作空间自动建立",
                deleted_at=row.deleted_at,
                created_at=now,
                updated_at=now,
            )
        )


def downgrade() -> None:
    # Backfilled projects may already have files or tasks; never delete user data
    # during a downgrade.  The rows remain valid platform projects.
    pass
