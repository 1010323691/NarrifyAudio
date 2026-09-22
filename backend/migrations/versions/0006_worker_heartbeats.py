"""persist worker health and capabilities

Revision ID: 0006_worker_heartbeats
Revises: 0005_session_workspace
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_worker_heartbeats"
down_revision = "0005_session_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "worker_heartbeats" in inspector.get_table_names():
        return
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=160), primary_key=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("current_task_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_worker_heartbeats_current_task_id", "worker_heartbeats", ["current_task_id"])
    op.create_index("ix_worker_heartbeats_last_seen_at", "worker_heartbeats", ["last_seen_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "worker_heartbeats" not in inspector.get_table_names():
        return
    op.drop_index("ix_worker_heartbeats_last_seen_at", table_name="worker_heartbeats")
    op.drop_index("ix_worker_heartbeats_current_task_id", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
