from __future__ import annotations

from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import User, UserQuotaAccount, Workspace, new_id
from .security import hash_password
from .storage import user_workspace_root


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
        workspace_id = new_id()
        db.add(
            Workspace(
                id=workspace_id,
                owner_id=user.id,
                name="默认工作空间",
                directory_key=f"{user.username}/{workspace_id}",
            )
        )
        db.flush()
        user_workspace_root(db, user.username, workspace_id).mkdir(parents=True, exist_ok=True)
