"""Persist retry eligibility for every task claiming path.

Revision ID: 0012_task_retry_eligibility
Revises: 0011_tts_quota_holds
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_task_retry_eligibility"
down_revision = "0011_tts_quota_holds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("tasks")}
    if "next_attempt_at" not in columns:
        op.add_column("tasks", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("tasks")}
    if "next_attempt_at" in columns:
        op.drop_column("tasks", "next_attempt_at")
