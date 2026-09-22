from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.config import settings
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context, require_csrf
from ..platform.models import AuditLog, Project, User, UserQuotaAccount, Workspace, new_id
from ..platform.quota_config import initial_quota_units
from ..platform.security import create_session, hash_password, revoke_session, verify_password
from ..platform.storage import user_workspace_root

router = APIRouter(prefix="/api/auth", tags=["auth"])
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=12, max_length=256)
    username: str | None = Field(default=None, min_length=3, max_length=64)
    display_name: str = Field(default="", max_length=120)


def _user_json(user: User) -> dict:
    return {"id": user.id, "email": user.email, "username": user.username, "display_name": user.display_name, "role": user.role, "is_active": user.is_active}


def _username(value: str | None, email: str) -> str:
    candidate = (value or email.split("@", 1)[0]).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", candidate):
        raise HTTPException(422, "用户名只能使用 3-64 位小写字母、数字、点、下划线或连字符")
    return candidate


def _set_cookies(response: Response, token: str, csrf: str) -> None:
    response.set_cookie(settings.session_cookie, token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_ttl_hours * 3600, path="/")
    response.set_cookie(settings.csrf_cookie, csrf, httponly=False, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_ttl_hours * 3600, path="/")


@router.post("/register", status_code=201)
def register(payload: Credentials, response: Response, db: Session = Depends(get_db)) -> dict:
    if not settings.registration_enabled:
        raise HTTPException(403, "当前已关闭注册")
    email = payload.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "邮箱格式不正确")
    username = _username(payload.username, email)
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(409, "邮箱已注册")
    if db.scalar(select(User).where(User.username == username)) is not None:
        raise HTTPException(409, "用户名已被占用")
    user = User(email=email, username=username, display_name=payload.display_name.strip(), password_hash=hash_password(payload.password), role="user")
    db.add(user)
    db.flush()
    db.add(UserQuotaAccount(user_id=user.id, available_units=initial_quota_units(db)))
    workspace_id = new_id()
    workspace = Workspace(
        id=workspace_id,
        owner_id=user.id,
        name="默认工作空间",
        directory_key=f"{user.username}/{workspace_id}",
    )
    db.add(workspace)
    db.add(Project(id=workspace_id, owner_id=user.id, name="默认工作空间", description="默认工作空间对应项目"))
    db.flush()
    user_workspace_root(db, user.username, workspace.id).mkdir(parents=True, exist_ok=True)
    token, csrf, session = create_session(db, user)
    session.active_workspace_id = workspace.id
    db.commit()
    _set_cookies(response, token, csrf)
    return {"user": _user_json(user), "csrf_token": csrf}


@router.post("/login")
def login(payload: Credentials, response: Response, db: Session = Depends(get_db)) -> dict:
    email = payload.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "邮箱或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账户已被禁用")
    token, csrf, _ = create_session(db, user)
    db.commit()
    _set_cookies(response, token, csrf)
    return {"user": _user_json(user), "csrf_token": csrf}


@router.get("/me")
def me(ctx: AuthContext = Depends(get_auth_context)) -> dict:
    return {"user": _user_json(ctx.user), "csrf_token": None}


@router.post("/logout")
def logout(response: Response, ctx: AuthContext = Depends(get_auth_context), _: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    revoke_session(ctx.session)
    db.commit()
    response.delete_cookie(settings.session_cookie, path="/")
    response.delete_cookie(settings.csrf_cookie, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(payload: Credentials, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    user.password_hash = hash_password(payload.password)
    for session in user.sessions:
        revoke_session(session)
    db.add(AuditLog(actor_user_id=user.id, action="auth.password_changed", target_type="user", target_id=user.id))
    db.commit()
    return {"ok": True, "message": "密码已修改，请重新登录"}
