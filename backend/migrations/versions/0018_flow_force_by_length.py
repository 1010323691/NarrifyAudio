"""add force_by_length to text_format_flows

「按字数分册」选项：用户可强制按目标字数分册（即使文本能识别出章节），
与 whole_book（整本输出，历史数据兼容）并列。旧行默认 False =
既有智能分册/自动兜底行为不变。

Revision ID: 0018_flow_force_by_length
Revises: 0017_text_format_workbench
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_flow_force_by_length"
down_revision = "0017_text_format_workbench"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "text_format_flows" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("text_format_flows")}
        if "force_by_length" not in columns:
            op.add_column(
                "text_format_flows",
                sa.Column("force_by_length", sa.Boolean(), nullable=False, server_default=sa.false()),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "text_format_flows" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("text_format_flows")}
        if "force_by_length" in columns:
            op.drop_column("text_format_flows", "force_by_length")
