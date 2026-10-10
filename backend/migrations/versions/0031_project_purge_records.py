"""Receipts of permanently deleted projects, re-verified the next day."""
from alembic import op
import sqlalchemy as sa

revision = '0031_project_purge_records'
down_revision = '0030_system_metric_samples'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('project_purge_records',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), nullable=False),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('directory_key', sa.String(255), nullable=False),
        sa.Column('reason', sa.String(20), nullable=False),
        sa.Column('export_task_ids', sa.JSON(), nullable=False),
        sa.Column('purged_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True)),
        sa.Column('leftovers', sa.JSON()),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.create_index('ix_project_purge_records_project_id', 'project_purge_records', ['project_id'])
    op.create_index('ix_project_purge_records_purged_at', 'project_purge_records', ['purged_at'])


def downgrade():
    op.drop_index('ix_project_purge_records_purged_at', table_name='project_purge_records')
    op.drop_index('ix_project_purge_records_project_id', table_name='project_purge_records')
    op.drop_table('project_purge_records')
