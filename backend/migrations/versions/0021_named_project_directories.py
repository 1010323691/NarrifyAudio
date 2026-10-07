"""Allow username plus the full user-chosen project name in directory keys.

Filesystem data relocation is performed by backend.services.workspace_migration,
separately from transactional schema changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_named_project_directories"
down_revision = "0020_flow_source_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.alter_column("directory_key", existing_type=sa.String(180), type_=sa.String(255), existing_nullable=False)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM projects WHERE length(directory_key) > 180")):
        raise RuntimeError("请先缩短项目名称，再回退目录字段长度")
    with op.batch_alter_table("projects") as batch:
        batch.alter_column("directory_key", existing_type=sa.String(255), type_=sa.String(180), existing_nullable=False)
