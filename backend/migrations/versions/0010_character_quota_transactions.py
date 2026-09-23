"""record resource and character details in quota transactions

Revision ID: 0010_character_quota
Revises: 0009_fair_scheduler_cursor
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_character_quota"
down_revision = "0009_fair_scheduler_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("quota_transactions")}
    additions = (
        ("resource_type", sa.String(length=10)),
        ("operation_type", sa.String(length=80)),
        ("char_count", sa.Integer()),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("quota_transactions", sa.Column(name, column, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("quota_transactions")}
    for name in ("char_count", "operation_type", "resource_type"):
        if name in columns:
            op.drop_column("quota_transactions", name)
