"""Batch receipts, shared configuration and compact task state."""
from alembic import op
import sqlalchemy as sa

revision = "0023_batch_submission"
down_revision = "0022_task_center_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("task_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_task_batches_owner_key"),
    )
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("batch_id", sa.String(36)))
        batch.add_column(sa.Column("event_sequence", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("admission_units", sa.Integer(), server_default="1", nullable=False))
        batch.add_column(sa.Column("ui_state", sa.JSON(), nullable=True))
        batch.create_foreign_key("fk_tasks_batch", "task_batches", ["batch_id"], ["id"])
        batch.create_index("ix_tasks_batch_id", ["batch_id"])
    op.execute("UPDATE tasks SET event_sequence = COALESCE((SELECT MAX(sequence) FROM task_events WHERE task_events.task_id = tasks.id), 0)")
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("UPDATE tasks SET admission_units = CASE WHEN json_typeof(payload->'scripts') = 'array' THEN GREATEST(1, json_array_length(payload->'scripts')) ELSE 1 END WHERE task_type = 'tts.batch'")
    else:
        op.execute("UPDATE tasks SET admission_units = CASE WHEN json_type(payload, '$.scripts') = 'array' THEN MAX(1, json_array_length(payload, '$.scripts')) ELSE 1 END WHERE task_type = 'tts.batch'")


def downgrade():
    connection = op.get_bind()
    tasks = sa.table('tasks', sa.column('id', sa.String), sa.column('batch_id', sa.String),
                     sa.column('payload', sa.JSON), sa.column('status', sa.String))
    batches = sa.table('task_batches', sa.column('id', sa.String), sa.column('config', sa.JSON))
    if connection.scalar(sa.select(tasks.c.id).where(tasks.c.batch_id.is_not(None),
        tasks.c.status.in_(['pending', 'queued', 'running', 'paused', 'cancelling', 'retrying'])).limit(1)):
        raise RuntimeError('请先停止新提交并排空合成任务，再降级批量提交迁移')
    # Older workers expect inline config; retain immutable snapshots on rollback.
    after = ''
    while True:
        rows = connection.execute(sa.select(tasks.c.id, tasks.c.payload, batches.c.config)
            .join(batches, tasks.c.batch_id == batches.c.id).where(tasks.c.id > after)
            .order_by(tasks.c.id).limit(100)).all()
        if not rows:
            break
        updates = []
        for task_id, payload, config in rows:
            restored = {**payload, 'config': config}
            restored.pop('_batch_config_id', None)
            updates.append({'task_key': task_id, 'restored_payload': restored})
        connection.execute(tasks.update().where(tasks.c.id == sa.bindparam('task_key'))
                           .values(payload=sa.bindparam('restored_payload', type_=sa.JSON)), updates)
        after = rows[-1][0]
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_batch_id")
        batch.drop_constraint("fk_tasks_batch", type_="foreignkey")
        batch.drop_column("ui_state")
        batch.drop_column("event_sequence")
        batch.drop_column("admission_units")
        batch.drop_column("batch_id")
    op.drop_table("task_batches")
