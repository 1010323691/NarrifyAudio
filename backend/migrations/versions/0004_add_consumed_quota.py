"""track settled quota consumption separately from reservations

Revision ID: 0004_add_consumed_quota
Revises: 0003_align_scope_constraints
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_add_consumed_quota"
down_revision = "0003_align_scope_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("user_quota_accounts")}
    if "consumed_units" not in columns:
        op.add_column(
            "user_quota_accounts",
            sa.Column("consumed_units", sa.Integer(), nullable=False, server_default="0"),
        )
        with op.batch_alter_table("user_quota_accounts") as batch:
            batch.alter_column("consumed_units", server_default=None)

    with op.batch_alter_table("user_quota_accounts") as batch:
        try:
            batch.drop_constraint("ck_quota_nonnegative", type_="check")
        except (sa.exc.OperationalError, sa.exc.CompileError):
            pass
        batch.create_check_constraint(
            "ck_quota_nonnegative",
            "available_units >= 0 and reserved_units >= 0 and frozen_units >= 0 and consumed_units >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("user_quota_accounts") as batch:
        try:
            batch.drop_constraint("ck_quota_nonnegative", type_="check")
        except (sa.exc.OperationalError, sa.exc.CompileError):
            pass
        batch.create_check_constraint(
            "ck_quota_nonnegative",
            "available_units >= 0 and reserved_units >= 0 and frozen_units >= 0",
        )
        batch.drop_column("consumed_units")
