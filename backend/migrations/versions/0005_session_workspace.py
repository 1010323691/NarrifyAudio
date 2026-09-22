"""bind sessions to managed user workspaces

Revision ID: 0005_session_workspace
Revises: 0004_add_consumed_quota
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_session_workspace"
down_revision = "0004_add_consumed_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("user_sessions")}
    if "active_workspace_id" not in columns:
        op.add_column("user_sessions", sa.Column("active_workspace_id", sa.String(length=36), nullable=True))
    indexes = {item["name"] for item in inspector.get_indexes("user_sessions")}
    if "ix_user_sessions_active_workspace_id" not in indexes:
        op.create_index("ix_user_sessions_active_workspace_id", "user_sessions", ["active_workspace_id"])
    foreign_keys = {
        (item.get("name"), tuple(item.get("constrained_columns") or []))
        for item in inspector.get_foreign_keys("user_sessions")
    }
    if bind.dialect.name != "sqlite" and (
        "fk_user_sessions_active_workspace_id", ("active_workspace_id",)
    ) not in foreign_keys:
        op.create_foreign_key(
            "fk_user_sessions_active_workspace_id",
            "user_sessions",
            "workspaces",
            ["active_workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("user_sessions")}
    if bind.dialect.name != "sqlite" and any(
        set(item.get("constrained_columns") or []) == {"active_workspace_id"}
        for item in inspector.get_foreign_keys("user_sessions")
    ):
        op.drop_constraint("fk_user_sessions_active_workspace_id", "user_sessions", type_="foreignkey")
    if "ix_user_sessions_active_workspace_id" in indexes:
        op.drop_index("ix_user_sessions_active_workspace_id", table_name="user_sessions")
    if "active_workspace_id" in {item["name"] for item in inspector.get_columns("user_sessions")}:
        op.drop_column("user_sessions", "active_workspace_id")
