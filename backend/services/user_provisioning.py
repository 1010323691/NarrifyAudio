"""Account creation shared by self-registration and administrator provisioning."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.filenames import safe_filename
from ..platform.models import User, UserQuotaAccount
from ..platform.security import hash_password
from ..platform.storage import project_workspace_path, storage_username
from ..platform.system_config import initial_quota_units
from .projects import create_project

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProvisioningError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def normalize_username(value: str | None, email: str) -> str:
    candidate = (value or email.split("@", 1)[0]).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{5,19}", candidate):
        raise ProvisioningError(422, "用户名必须为 6–20 位小写字母、数字、点、下划线或连字符")
    if safe_filename(candidate) != candidate:
        raise ProvisioningError(422, "用户名不能使用 Windows 保留名称或以点结尾")
    return candidate


def validate_password(password: str) -> None:
    if not 6 <= len(password) <= 20:
        raise ProvisioningError(422, "密码必须为 6–20 个字符")


def provision_user(db: Session, *, email: str, username: str | None, password: str,
                   display_name: str = "", role: str = "user"):
    """Create the user, its quota account and default workspace (caller commits).

    Returns ``(user, default_project)``.
    """
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ProvisioningError(422, "邮箱格式不正确")
    validate_password(password)
    username = normalize_username(username, email)
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise ProvisioningError(409, "邮箱已注册")
    if db.scalar(select(User).where(User.username == username)) is not None:
        raise ProvisioningError(409, "用户名已被占用")
    if any(storage_username(existing) == storage_username(username) for existing in db.scalars(select(User.username))):
        raise ProvisioningError(409, "用户名存储目录已被占用")
    user = User(email=email, username=username, display_name=display_name.strip(), password_hash=hash_password(password), role=role)
    db.add(user)
    db.flush()
    db.add(UserQuotaAccount(user_id=user.id, available_units=initial_quota_units(db)))
    project = create_project(
        db, owner_id=user.id, username=user.username, name="默认工作空间", description="默认工作空间",
    )
    project_workspace_path(db, user.username, project.id).mkdir(parents=True, exist_ok=True)
    return user, project
