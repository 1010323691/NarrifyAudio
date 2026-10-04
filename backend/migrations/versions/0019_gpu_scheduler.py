"""Persistent single-GPU coordination and stage requests."""
from alembic import op
import sqlalchemy as sa

revision = "0019_gpu_scheduler"
down_revision = "0018_flow_force_by_length"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("gpu_scheduler_state", sa.Column("id", sa.String(32), primary_key=True),
                    sa.Column("value", sa.JSON(), nullable=False))
    op.create_table("gpu_requests", sa.Column("id", sa.String(36), primary_key=True),
                    sa.Column("task_id", sa.String(36)), sa.Column("attempt_id", sa.String(36)),
                    sa.Column("service", sa.String(8), nullable=False),
                    sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("owner_pid", sa.Integer(), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("process", sa.JSON(), nullable=False))
    op.create_index("ix_gpu_requests_task_id", "gpu_requests", ["task_id"])
    op.create_index("ix_gpu_requests_status", "gpu_requests", ["status"])


def downgrade():
    op.drop_table("gpu_requests")
    op.drop_table("gpu_scheduler_state")
