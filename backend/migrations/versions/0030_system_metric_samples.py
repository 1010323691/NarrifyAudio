"""Persisted 30-second platform resource samples for the admin console."""
from alembic import op
import sqlalchemy as sa

revision = '0030_system_metric_samples'
down_revision = '0029_progress_refresh'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('system_metric_samples',
        sa.Column('bucket', sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column('sampled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False))
    op.create_index('ix_system_metric_samples_sampled_at', 'system_metric_samples', ['sampled_at'])


def downgrade():
    op.drop_index('ix_system_metric_samples_sampled_at', table_name='system_metric_samples')
    op.drop_table('system_metric_samples')
