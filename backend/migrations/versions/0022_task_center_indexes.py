"""Indexes for bounded task-center lists and latest progress lookup."""
from alembic import op

revision = "0022_task_center_indexes"
down_revision = "0021_named_project_directories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_tasks_owner_created", "tasks", ["owner_id", "created_at", "id"])
    op.create_index("ix_tasks_entry_created", "tasks", ["owner_id", "project_id", "task_type", "created_at", "id"])
    op.create_index("ix_task_events_type_sequence", "task_events", ["task_id", "event_type", "sequence"])


def downgrade() -> None:
    op.drop_index("ix_task_events_type_sequence", table_name="task_events")
    op.drop_index("ix_tasks_entry_created", table_name="tasks")
    op.drop_index("ix_tasks_owner_created", table_name="tasks")
