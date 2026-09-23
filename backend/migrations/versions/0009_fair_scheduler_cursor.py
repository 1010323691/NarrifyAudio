"""add a persistent per-user scheduler cursor

Revision ID: 0009_fair_scheduler_cursor
Revises: 0008_workspace_projects
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_fair_scheduler_cursor"
down_revision = "0008_workspace_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_quota_accounts",
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_user_quota_accounts_last_scheduled_at",
        "user_quota_accounts",
        ["last_scheduled_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_quota_accounts_last_scheduled_at", table_name="user_quota_accounts")
    op.drop_column("user_quota_accounts", "last_scheduled_at")
