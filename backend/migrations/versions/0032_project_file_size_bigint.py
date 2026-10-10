"""project_files.size_bytes to BIGINT: packaged ZIPs can exceed 2 GiB."""
from alembic import op
import sqlalchemy as sa

revision = '0032_project_file_size_bigint'
down_revision = '0031_project_purge_records'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('project_files') as batch:
        batch.alter_column('size_bytes', existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=False)


def downgrade():
    with op.batch_alter_table('project_files') as batch:
        batch.alter_column('size_bytes', existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False)
