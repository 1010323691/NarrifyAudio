"""enforce unique names among active projects

Revision ID: 0016_unique_active_project_names
Revises: 0015_drop_quota_reservations
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_unique_active_project_names"
down_revision = "0015_drop_quota_reservations"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_projects_owner_active_name"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("projects")}
    if INDEX_NAME in indexes:
        return

    duplicates = bind.execute(sa.text(
        "SELECT owner_id, name, COUNT(*) AS project_count "
        "FROM projects WHERE deleted_at IS NULL "
        "GROUP BY owner_id, name HAVING COUNT(*) > 1 LIMIT 5"
    )).all()
    if duplicates:
        raise RuntimeError(
            "Cannot enforce unique active project names while duplicate names exist; "
            f"rename duplicates and retry the migration: {duplicates!r}"
        )

    op.create_index(
        INDEX_NAME,
        "projects",
        ["owner_id", "name"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("projects")}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="projects")
