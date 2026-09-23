"""add stable usernames and user-owned workspace metadata

Revision ID: 0002_usernames_paths
Revises: 0001_platform_foundation
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_usernames_paths"
down_revision = "0001_platform_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "username" not in columns:
        op.add_column("users", sa.Column("username", sa.String(length=64), nullable=True))
        op.execute("UPDATE users SET username = 'user-' || substr(replace(id, '-', ''), 1, 12) WHERE username IS NULL")
        with op.batch_alter_table("users") as batch:
            batch.alter_column("username", existing_type=sa.String(length=64), nullable=False)
        op.create_index("ix_users_username_unique", "users", ["username"], unique=True)

    workspace_tables = set(inspector.get_table_names())
    if "workspaces" not in workspace_tables:
        from backend.platform.models import Workspace

        Workspace.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "workspaces" in set(inspector.get_table_names()):
        from backend.platform.models import Workspace

        Workspace.__table__.drop(bind=bind, checkfirst=True)
    if "username" in {column["name"] for column in inspector.get_columns("users")}:
        op.drop_index("ix_users_username_unique", table_name="users")
        with op.batch_alter_table("users") as batch:
            batch.drop_column("username")
