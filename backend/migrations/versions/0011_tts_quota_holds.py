"""reserve TTS input characters while synthesis is running

Revision ID: 0011_tts_quota_holds
Revises: 0010_character_quota_transactions
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_tts_quota_holds"
down_revision = "0010_character_quota_transactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quota_holds",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("operation_type", sa.String(length=80), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="held"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quota_holds_user_id", "quota_holds", ["user_id"])
    op.create_index("ix_quota_holds_task_id", "quota_holds", ["task_id"])
    op.create_index("ix_quota_holds_attempt_id", "quota_holds", ["attempt_id"])
    op.create_index("uq_quota_hold_attempt_operation", "quota_holds", ["attempt_id", "operation_type"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_quota_hold_attempt_operation", table_name="quota_holds")
    op.drop_index("ix_quota_holds_attempt_id", table_name="quota_holds")
    op.drop_index("ix_quota_holds_task_id", table_name="quota_holds")
    op.drop_index("ix_quota_holds_user_id", table_name="quota_holds")
    op.drop_table("quota_holds")
