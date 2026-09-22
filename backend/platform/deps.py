from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User, UserSession
from .security import load_session, token_digest


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: UserSession


def get_auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    session = load_session(db, request.cookies.get(settings.session_cookie))
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")
    return AuthContext(user=session.user, session=session)


def require_user(ctx: AuthContext = Depends(get_auth_context)) -> User:
    return ctx.user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def require_csrf(request: Request, ctx: AuthContext = Depends(get_auth_context)) -> User:
    # The CSRF cookie is intentionally readable by the browser; requiring the
    # header makes a cross-site form submission insufficient even when the
    # session cookie is attached.
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied or not hmac.compare_digest(token_digest(supplied), ctx.session.csrf_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")
    return ctx.user
