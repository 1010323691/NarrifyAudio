"""platform foundation

Revision ID: 0001_platform_foundation
Revises:
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_platform_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical revisions must not change when the live ORM grows new tables.
    from backend.migrations.foundation_schema import Base
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    from backend.migrations.foundation_schema import Base
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
