"""reserve TTS input characters while synthesis is running

Revision ID: 0011_tts_quota_holds
Revises: 0010_character_quota
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_tts_quota_holds"
down_revision = "0010_character_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "quota_holds" not in inspector.get_table_names():
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
    else:
        required = {"id", "user_id", "task_id", "attempt_id", "operation_type", "units", "status", "created_at"}
        actual = {column["name"] for column in inspector.get_columns("quota_holds")}
        if missing := required - actual:
            raise RuntimeError(f"Existing quota_holds table is missing columns: {sorted(missing)}")
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("quota_holds")}
    for name, columns, unique in (
        ("ix_quota_holds_user_id", ["user_id"], False),
        ("ix_quota_holds_task_id", ["task_id"], False),
        ("ix_quota_holds_attempt_id", ["attempt_id"], False),
        ("uq_quota_hold_attempt_operation", ["attempt_id", "operation_type"], True),
    ):
        if name not in indexes:
            op.create_index(name, "quota_holds", columns, unique=unique)


def downgrade() -> None:
    op.drop_index("uq_quota_hold_attempt_operation", table_name="quota_holds")
    op.drop_index("ix_quota_holds_attempt_id", table_name="quota_holds")
    op.drop_index("ix_quota_holds_task_id", table_name="quota_holds")
    op.drop_index("ix_quota_holds_user_id", table_name="quota_holds")
    op.drop_table("quota_holds")
