"""Indexes for eligible recovery pages and active-lease exclusion."""
from alembic import op

revision = "0028_recovery_indexes"
down_revision = "0027_current_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_tasks_recovery_page", "tasks", ["status", "id"])
    op.create_index("ix_task_attempts_live_lease", "task_attempts", ["task_id", "status", "lease_expires_at"])


def downgrade():
    op.drop_index("ix_task_attempts_live_lease", table_name="task_attempts")
    op.drop_index("ix_tasks_recovery_page", table_name="tasks")
