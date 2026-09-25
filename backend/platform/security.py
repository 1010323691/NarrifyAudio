from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .platform_settings import settings
from .models import User, UserSession, utcnow


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    if not 6 <= len(password) <= 20:
        raise ValueError("密码必须为 6–20 个字符")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, maxmem=64 * 1024 * 1024)
    return f"scrypt$16384$8$1${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, _unb64(expected))
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive datetime round-trip to the UTC contract."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def create_session(db: Session, user: User) -> tuple[str, str, UserSession]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    session = UserSession(
        user_id=user.id,
        token_hash=token_digest(token),
        csrf_hash=token_digest(csrf),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(session)
    return token, csrf, session


def load_session(db: Session, token: str | None, *, touch: bool = True) -> UserSession | None:
    if not token:
        return None
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_digest(token)))
    if session is None or session.revoked_at is not None or _as_utc(session.expires_at) <= utcnow():
        return None
    if not session.user.is_active:
        return None
    if touch:
        session.last_seen_at = utcnow()
    return session


def session_is_valid_for_user(db: Session, token: str | None, user_id: str) -> bool:
    """Recheck an already-open request's session without trusting its stale context."""
    # Long-lived stream checks are authorization reads, not new user activity.
    # Avoid dirtying last_seen_at in a short-lived session that will roll back.
    session = load_session(db, token, touch=False)
    return session is not None and session.user_id == user_id


def revoke_session(session: UserSession) -> None:
    session.revoked_at = utcnow()
