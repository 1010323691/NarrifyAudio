"""Durable coalesced progress refresh and start-time throttle."""
from alembic import op
import sqlalchemy as sa

revision = '0029_progress_refresh'
down_revision = '0028_recovery_indexes'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('project_progress_refresh',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('requested_signature', sa.String(64), nullable=False),
        sa.Column('dirty', sa.Boolean(), nullable=False),
        sa.Column('task_id', sa.String(36)),
        sa.Column('last_started_at', sa.DateTime(timezone=True)),
        sa.Column('next_due_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_progress_refresh_due', 'project_progress_refresh', ['dirty', 'next_due_at', 'project_id'])


def downgrade():
    op.drop_index('ix_progress_refresh_due', table_name='project_progress_refresh')
    op.drop_table('project_progress_refresh')
