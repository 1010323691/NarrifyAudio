"""Seed the bootstrap admin account and its default workspace at startup.

Lives in the services layer (not platform): combining platform primitives
(user, quota account, workspace path) with project creation is a service
concern, and platform must not import services (S3 direction, Q17).
"""
from __future__ import annotations

from sqlalchemy import select

from ..platform.config import settings
from ..platform.database import SessionLocal
from ..platform.models import User, UserQuotaAccount
from ..platform.security import hash_password
from ..platform.storage import project_workspace_path
from .projects import create_project


def ensure_bootstrap_admin() -> None:
    email = settings.bootstrap_admin_email.strip().lower()
    password = settings.bootstrap_admin_password
    if not email or not password:
        return
    with SessionLocal.begin() as db:
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            return
        username = email.split("@", 1)[0].lower()
        if not username or not username[0].isalnum():
            username = "admin"
        user = User(email=email, username=username[:64], display_name="Administrator", password_hash=hash_password(password), role="admin")
        db.add(user)
        db.flush()
        db.add(UserQuotaAccount(user_id=user.id, available_units=0))
        project = create_project(
            db, owner_id=user.id, username=user.username,
            name="默认工作空间", description="默认工作空间",
        )
        db.flush()
        project_workspace_path(db, user.username, project.id).mkdir(parents=True, exist_ok=True)
