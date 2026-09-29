"""add text_format_flows and chapter_review_marks

Revision ID: 0017_text_format_workbench
Revises: 0016_unique_active_project_names
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_text_format_workbench"
down_revision = "0016_unique_active_project_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "text_format_flows" not in existing:
        op.create_table(
            "text_format_flows",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("owner_id", sa.String(length=36), nullable=False),
            sa.Column("source_file_id", sa.String(length=36), nullable=False),
            sa.Column("config_snapshot", sa.JSON(), nullable=False),
            sa.Column("whole_book", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("format_task_id", sa.String(length=36), nullable=True),
            sa.Column("analyze_task_id", sa.String(length=36), nullable=True),
            sa.Column("split_task_id", sa.String(length=36), nullable=True),
            sa.Column("split_mode", sa.String(length=20), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("manifest", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["project_id", "owner_id"], ["projects.id", "projects.owner_id"],
                name="fk_text_format_flows_project_owner",
            ),
            sa.CheckConstraint("status in ('running','ready','failed')", name="ck_text_format_flows_status"),
            sa.CheckConstraint(
                "split_mode in ('smart','by_length','whole_book') or split_mode is null",
                name="ck_text_format_flows_split_mode",
            ),
        )
        op.create_index("ix_text_format_flows_project_id", "text_format_flows", ["project_id"])
        op.create_index("ix_text_format_flows_owner_id", "text_format_flows", ["owner_id"])
        op.create_index("ix_text_format_flows_status", "text_format_flows", ["status"])
        op.create_index("ix_text_format_flows_project_updated", "text_format_flows", ["project_id", "updated_at"])

    if "chapter_review_marks" not in existing:
        op.create_table(
            "chapter_review_marks",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("owner_id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("chapter_key", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["project_id", "owner_id"], ["projects.id", "projects.owner_id"],
                name="fk_chapter_review_marks_project_owner",
            ),
        )
        # Named UNIQUE index (not an inline constraint): the name is portable —
        # SQLite leaves inline UNIQUE constraint indexes unnamed, which breaks
        # inspection on the sqlite test databases.
        op.create_index("uq_review_marks_task_chapter", "chapter_review_marks", ["task_id", "chapter_key"], unique=True)
        op.create_index("ix_chapter_review_marks_project_id", "chapter_review_marks", ["project_id"])
        op.create_index("ix_chapter_review_marks_owner_id", "chapter_review_marks", ["owner_id"])
        op.create_index("ix_review_marks_project_task", "chapter_review_marks", ["project_id", "task_id"])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "chapter_review_marks" in existing:
        op.drop_index("ix_review_marks_project_task", table_name="chapter_review_marks")
        op.drop_index("ix_chapter_review_marks_owner_id", table_name="chapter_review_marks")
        op.drop_index("ix_chapter_review_marks_project_id", table_name="chapter_review_marks")
        op.drop_table("chapter_review_marks")
    if "text_format_flows" in existing:
        op.drop_index("ix_text_format_flows_project_updated", table_name="text_format_flows")
        op.drop_index("ix_text_format_flows_status", table_name="text_format_flows")
        op.drop_index("ix_text_format_flows_owner_id", table_name="text_format_flows")
        op.drop_index("ix_text_format_flows_project_id", table_name="text_format_flows")
        op.drop_table("text_format_flows")
