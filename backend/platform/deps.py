from __future__ import annotations

import hmac
import threading
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .platform_settings import settings
from .database import get_db, SessionLocal
from .models import User, UserSession
from .project_context import active_project
from .security import load_session, token_digest
from .storage import project_workspace_path
from ..core.request_context import bind_workspace


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: UserSession


def get_auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    session = load_session(db, request.cookies.get(settings.session_cookie))
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")
    if request.url.path.startswith('/api/v1/tasks'):
        return AuthContext(user=session.user, session=session)
    project = active_project(db, session.user, session)
    bind_workspace(
        project_workspace_path(db, session.user.username, project.id)
        if project is not None
        else None
    )
    return AuthContext(user=session.user, session=session)


def require_authenticated_user(ctx: AuthContext = Depends(get_auth_context)) -> User:
    return ctx.user


_stream_lock = threading.Lock()
_streams: dict[str, int] = {}


def require_stream_user(request: Request):
    """Authenticate without pinning a database connection for the stream lifetime."""
    with SessionLocal() as db:
        session = load_session(db, request.cookies.get(settings.session_cookie), touch=False)
        if session is None:
            raise HTTPException(401, "需要登录")
        user = session.user
        db.expunge(user)
    with _stream_lock:
        if _streams.get(user.id, 0) >= 3:
            raise HTTPException(429, "任务观察连接过多", headers={"Retry-After": "10"})
        _streams[user.id] = _streams.get(user.id, 0) + 1
    try:
        yield user
    finally:
        with _stream_lock:
            count = _streams.get(user.id, 1) - 1
            if count:
                _streams[user.id] = count
            else:
                _streams.pop(user.id, None)


def require_admin(user: User = Depends(require_authenticated_user)) -> User:
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


def require_admin_csrf(request: Request, user: User = Depends(require_csrf)) -> User:
    """Admin-gated counterpart of :func:`require_csrf` for state-changing routes."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def require_legacy_access(
    request: Request, ctx: AuthContext = Depends(get_auth_context)
) -> User:
    """Authenticate legacy routes and require CSRF for state-changing calls."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return ctx.user
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied or not hmac.compare_digest(token_digest(supplied), ctx.session.csrf_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")
    return ctx.user
