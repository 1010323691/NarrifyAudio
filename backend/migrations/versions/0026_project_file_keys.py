"""Indexed portable object keys; preserve every historical collision."""
import unicodedata
import hashlib
from alembic import op
import sqlalchemy as sa

revision = "0026_project_file_keys"
down_revision = "0025_mechanical_audio_permits"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("project_files", sa.Column("object_key_normalized", sa.String(64), nullable=True))
    connection = op.get_bind()
    files = sa.table("project_files", sa.column("id", sa.String), sa.column("object_key", sa.String),
                     sa.column("object_key_normalized", sa.String))
    cursor = ""
    while True:
        rows = connection.execute(sa.select(files.c.id, files.c.object_key).where(files.c.id > cursor)
                                  .order_by(files.c.id).limit(100)).all()
        if not rows:
            break
        connection.execute(files.update().where(files.c.id == sa.bindparam("row_id"))
                           .values(object_key_normalized=sa.bindparam("folded")),
                           [{"row_id": row.id, "folded": hashlib.sha256(unicodedata.normalize("NFC", row.object_key.replace("\\", "/")).casefold().encode("utf-8")).hexdigest()}
                            for row in rows])
        cursor = rows[-1].id
    with op.batch_alter_table("project_files") as batch:
        batch.alter_column("object_key_normalized", existing_type=sa.String(64), nullable=False)
        batch.create_index("ix_project_files_normalized", ["project_id", "object_key_normalized"], unique=False)


def downgrade():
    with op.batch_alter_table("project_files") as batch:
        batch.drop_index("ix_project_files_normalized")
        batch.drop_column("object_key_normalized")
