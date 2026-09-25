from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import Project, User, UserSession
from .security import load_session, token_digest
from .storage import user_workspace_root
from ..core.request_context import bind_workspace


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: UserSession


def get_auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    session = load_session(db, request.cookies.get(settings.session_cookie))
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")
    project = None
    if session.active_project_id:
        project = db.scalar(
            select(Project).where(
                Project.id == session.active_project_id,
                Project.owner_id == session.user_id,
                Project.deleted_at.is_(None),
            )
        )
    if project is None:
        project = db.scalar(
            select(Project)
            .where(Project.owner_id == session.user_id, Project.deleted_at.is_(None))
            .order_by(Project.last_selected_at.desc().nullslast(), Project.updated_at.desc())
        )
    bind_workspace(
        user_workspace_root(db, session.user.username, project.id)
        if project is not None
        else None
    )
    return AuthContext(user=session.user, session=session)


def require_authenticated_user(ctx: AuthContext = Depends(get_auth_context)) -> User:
    return ctx.user


require_user = require_authenticated_user  # compatibility for direct Python imports


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
