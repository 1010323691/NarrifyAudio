"""retire the task-level quota_reservations table

Revision ID: 0015_drop_quota_reservations
Revises: 0014_project_storage_identity
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_drop_quota_reservations"
down_revision = "0014_project_storage_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quota_reservations" not in inspector.get_table_names():
        return
    if "reservation_id" in {column["name"] for column in inspector.get_columns("quota_transactions")}:
        # Dead with the reservation flow: only settle/release_reservation ever
        # wrote it. Drop the dependent FK + index + column together with the table.
        # FK constraints cannot be altered on SQLite (0007 never creates them there).
        if bind.dialect.name != "sqlite":
            for fk in inspector.get_foreign_keys("quota_transactions"):
                if fk["name"] == "fk_quota_transactions_reservation_id":
                    op.drop_constraint(fk["name"], "quota_transactions", type_="foreignkey")
        if "ix_quota_transactions_reservation_id" in {
            index["name"] for index in inspector.get_indexes("quota_transactions")
        }:
            op.drop_index("ix_quota_transactions_reservation_id", table_name="quota_transactions")
        op.drop_column("quota_transactions", "reservation_id")
    op.drop_table("quota_reservations")


def downgrade() -> None:
    # Structural rollback only: rows dropped by upgrade cannot be restored
    # (plan A1 — data rollback requires the pre-migration database backup).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quota_reservations" not in inspector.get_table_names():
        op.create_table(
            "quota_reservations",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("units", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="reserved"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("settled_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("task_id"),
        )
        op.create_index("ix_quota_reservations_user_id", "quota_reservations", ["user_id"])
    if "reservation_id" not in {column["name"] for column in inspector.get_columns("quota_transactions")}:
        op.add_column("quota_transactions", sa.Column("reservation_id", sa.String(length=36), nullable=True))
        op.create_index("ix_quota_transactions_reservation_id", "quota_transactions", ["reservation_id"])
    if bind.dialect.name != "sqlite":
        existing = {
            fk["name"]
            for fk in sa.inspect(bind).get_foreign_keys("quota_transactions")
        }
        if "fk_quota_transactions_reservation_id" not in existing:
            op.create_foreign_key(
                "fk_quota_transactions_reservation_id", "quota_transactions", "quota_reservations",
                ["reservation_id"], ["id"], ondelete="RESTRICT",
            )
