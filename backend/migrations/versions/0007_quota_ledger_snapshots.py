"""add auditable quota balance snapshots and actor references

Revision ID: 0007_quota_ledger_snapshots
Revises: 0006_worker_heartbeats
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_quota_ledger_snapshots"
down_revision = "0006_worker_heartbeats"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("reservation_id", sa.String(length=36)),
    ("actor_user_id", sa.String(length=36)),
    ("available_before", sa.Integer()),
    ("available_after", sa.Integer()),
    ("reserved_before", sa.Integer()),
    ("reserved_after", sa.Integer()),
    ("consumed_before", sa.Integer()),
    ("consumed_after", sa.Integer()),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {item["name"] for item in inspector.get_columns("quota_transactions")}
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column("quota_transactions", sa.Column(name, column, nullable=True))
    existing_indexes = {item["name"] for item in inspector.get_indexes("quota_transactions")}
    if "ix_quota_transactions_reservation_id" not in existing_indexes:
        op.create_index("ix_quota_transactions_reservation_id", "quota_transactions", ["reservation_id"])
    if "ix_quota_transactions_actor_user_id" not in existing_indexes:
        op.create_index("ix_quota_transactions_actor_user_id", "quota_transactions", ["actor_user_id"])
    if bind.dialect.name != "sqlite":
        foreign_keys = {item.get("name") for item in inspector.get_foreign_keys("quota_transactions")}
        if "fk_quota_transactions_reservation_id" not in foreign_keys:
            op.create_foreign_key("fk_quota_transactions_reservation_id", "quota_transactions", "quota_reservations", ["reservation_id"], ["id"], ondelete="RESTRICT")
        if "fk_quota_transactions_actor_user_id" not in foreign_keys:
            op.create_foreign_key("fk_quota_transactions_actor_user_id", "quota_transactions", "users", ["actor_user_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_constraint("fk_quota_transactions_actor_user_id", "quota_transactions", type_="foreignkey")
        op.drop_constraint("fk_quota_transactions_reservation_id", "quota_transactions", type_="foreignkey")
    op.drop_index("ix_quota_transactions_actor_user_id", table_name="quota_transactions")
    op.drop_index("ix_quota_transactions_reservation_id", table_name="quota_transactions")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("quota_transactions", name)
