from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.platform_settings import settings
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context, require_csrf
from ..platform.models import User
from ..platform.system_config import registration_enabled
from ..platform.security import create_session, revoke_session, verify_password
from ..services.user_provisioning import EMAIL_RE, ProvisioningError, provision_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    email: str | None = None
    identifier: str | None = Field(default=None, max_length=320)
    password: str = Field(max_length=256)
    username: str | None = Field(default=None, min_length=6, max_length=20)
    display_name: str = Field(default="", max_length=120)


def _user_json(user: User) -> dict:
    return {"id": user.id, "email": user.email, "username": user.username, "display_name": user.display_name, "role": user.role, "is_active": user.is_active}


def _set_cookies(response: Response, token: str, csrf: str) -> None:
    response.set_cookie(settings.session_cookie, token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_ttl_hours * 3600, path="/")
    response.set_cookie(settings.csrf_cookie, csrf, httponly=False, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_ttl_hours * 3600, path="/")


@router.post("/register", status_code=201)
def register(payload: Credentials, response: Response, db: Session = Depends(get_db)) -> dict:
    if not registration_enabled(db):
        raise HTTPException(403, "当前已关闭注册")
    try:
        user, project = provision_user(db, email=payload.email or "", username=payload.username,
                                       password=payload.password, display_name=payload.display_name)
    except ProvisioningError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
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
