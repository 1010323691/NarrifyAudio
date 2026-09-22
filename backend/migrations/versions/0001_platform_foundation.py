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
    # The declarative metadata is the source for the full schema.  The first
    # migration is intentionally generated from it so an empty PostgreSQL
    # database and local auto-create mode have the same contract.
    from backend.platform.database import Base
    from backend.platform import models  # noqa: F401
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    from backend.platform.database import Base
    from backend.platform import models  # noqa: F401
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)

