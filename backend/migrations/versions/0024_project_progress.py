"""Persist project production progress independently of page requests."""
from alembic import op
import sqlalchemy as sa

revision = "0024_project_progress"
down_revision = "0023_batch_submission"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_progress",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("stages", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("project_progress")
