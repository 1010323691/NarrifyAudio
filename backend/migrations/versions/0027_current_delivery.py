"""Current delivery authority and resumable historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = "0027_current_delivery"
down_revision = "0026_project_file_keys"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("current_deliveries",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("path_key", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("relative_path", sa.String(700), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("identity", sa.JSON()),
        sa.Column("valid", sa.Boolean(), nullable=False))
    op.create_index("ix_current_deliveries_owner_project", "current_deliveries", ["owner_id", "project_id"])
    op.create_table("delivery_index_state",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("cursor", sa.String(36), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("delivery_index_state")
    op.drop_index("ix_current_deliveries_owner_project", table_name="current_deliveries")
    op.drop_table("current_deliveries")
