"""Persist the ordered source documents of a text-format flow."""
from alembic import op
import sqlalchemy as sa

revision = "0020_flow_source_files"
down_revision = "0019_gpu_scheduler"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("text_format_flows")}
    if "source_file_ids" not in columns:
        op.add_column("text_format_flows", sa.Column("source_file_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("text_format_flows")}
    if "source_file_ids" in columns:
        op.drop_column("text_format_flows", "source_file_ids")
