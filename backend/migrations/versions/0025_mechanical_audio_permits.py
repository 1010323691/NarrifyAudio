"""Host-wide mechanical audio admission, independent of worker count."""
from alembic import op
import sqlalchemy as sa

revision = "0025_mechanical_audio_permits"
down_revision = "0024_project_progress"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mechanical_audio_state",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("memory_paused", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
    )
    op.create_table(
        "mechanical_audio_permits",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), nullable=False, unique=True),
        sa.Column("process", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM mechanical_audio_permits")):
        raise RuntimeError("停止音频 Worker 并确认子进程退出、释放许可后才能降级")
    op.drop_table("mechanical_audio_permits")
    op.drop_table("mechanical_audio_state")
