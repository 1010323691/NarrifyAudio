"""add a persistent per-user scheduler cursor

Revision ID: 0009_fair_scheduler_cursor
Revises: 0008_workspace_projects
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0009_fair_scheduler_cursor"
down_revision = "0008_workspace_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("user_quota_accounts")}
    indexes = {index["name"] for index in inspector.get_indexes("user_quota_accounts")}

    if "last_scheduled_at" not in columns:
        op.add_column(
            "user_quota_accounts",
            sa.Column("last_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "ix_user_quota_accounts_last_scheduled_at" not in indexes:
        op.create_index(
            "ix_user_quota_accounts_last_scheduled_at",
            "user_quota_accounts",
            ["last_scheduled_at"],
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("user_quota_accounts")}
    columns = {column["name"] for column in inspector.get_columns("user_quota_accounts")}

    if "ix_user_quota_accounts_last_scheduled_at" in indexes:
        op.drop_index("ix_user_quota_accounts_last_scheduled_at", table_name="user_quota_accounts")
    if "last_scheduled_at" in columns:
        op.drop_column("user_quota_accounts", "last_scheduled_at")
