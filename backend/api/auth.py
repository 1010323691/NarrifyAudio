from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.platform_settings import settings
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context, require_csrf
from ..platform.models import User, UserQuotaAccount
from ..platform.system_config import initial_quota_units, registration_enabled
from ..platform.security import create_session, hash_password, revoke_session, verify_password
from ..platform.storage import project_workspace_path
from ..services.projects import create_project

router = APIRouter(prefix="/api/auth", tags=["auth"])
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Credentials(BaseModel):
    email: str | None = None
    identifier: str | None = Field(default=None, max_length=320)
    password: str = Field(max_length=256)
    username: str | None = Field(default=None, min_length=6, max_length=20)
    display_name: str = Field(default="", max_length=120)


def _user_json(user: User) -> dict:
    return {"id": user.id, "email": user.email, "username": user.username, "display_name": user.display_name, "role": user.role, "is_active": user.is_active}


def _username(value: str | None, email: str) -> str:
    candidate = (value or email.split("@", 1)[0]).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{5,19}", candidate):
        raise HTTPException(422, "用户名必须为 6–20 位小写字母、数字、点、下划线或连字符")
    return candidate


def _validate_password(password: str) -> None:
    if not 6 <= len(password) <= 20:
        raise HTTPException(422, "密码必须为 6–20 个字符")


def _set_cookies(response: Response, token: str, csrf: str) -> None:
    response.set_cookie(settings.session_cookie, token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_ttl_hours * 3600, path="/")
    response.set_cookie(settings.csrf_cookie, csrf, httponly=False, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_ttl_hours * 3600, path="/")


@router.post("/register", status_code=201)
def register(payload: Credentials, response: Response, db: Session = Depends(get_db)) -> dict:
    if not registration_enabled(db):
        raise HTTPException(403, "当前已关闭注册")
    email = (payload.email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "邮箱格式不正确")
    _validate_password(payload.password)
    username = _username(payload.username, email)
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(409, "邮箱已注册")
    if db.scalar(select(User).where(User.username == username)) is not None:
        raise HTTPException(409, "用户名已被占用")
    user = User(email=email, username=username, display_name=payload.display_name.strip(), password_hash=hash_password(payload.password), role="user")
    db.add(user)
    db.flush()
    db.add(UserQuotaAccount(user_id=user.id, available_units=initial_quota_units(db)))
    project = create_project(
        db, owner_id=user.id, username=user.username, name="默认工作空间", description="默认工作空间",
    )
    project_workspace_path(db, user.username, project.id).mkdir(parents=True, exist_ok=True)
    token, csrf, session = create_session(db, user)
    session.active_project_id = project.id
    db.commit()
    _set_cookies(response, token, csrf)
    return {"user": _user_json(user), "csrf_token": csrf}


@router.post("/login")
def login(payload: Credentials, response: Response, db: Session = Depends(get_db)) -> dict:
    identifier = (payload.identifier or payload.email or "").strip().lower()
    if EMAIL_RE.match(identifier):
        user = db.scalar(select(User).where(User.email == identifier))
    else:
        user = db.scalar(select(User).where(User.username == identifier))
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
