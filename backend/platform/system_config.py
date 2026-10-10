"""SystemConfig-key accessors: quota defaults, registration, feature defaults.

One module per config domain — these three keys are all administrator-managed
SystemConfig reads, and the feature cache must live beside its readers.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from .platform_settings import settings
from .database import SessionLocal
from .models import SystemConfig
from ..core.config import set_platform_defaults_provider


def initial_quota_units(db: Session) -> int:
    config = db.get(SystemConfig, "quota.initial_units")
    if config is None or not isinstance(config.value, dict):
        return max(0, settings.initial_quota_units)
    try:
        return max(0, int(config.value.get("units", settings.initial_quota_units)))
    except (TypeError, ValueError):
        return max(0, settings.initial_quota_units)


def registration_enabled(db: Session) -> bool:
    config = db.get(SystemConfig, "registration.enabled")
    if config is None or not isinstance(config.value, dict):
        return settings.registration_enabled
    value = config.value.get("enabled", settings.registration_enabled)
    return bool(value)


_lock = threading.RLock()
_cache: dict[str, Any] = {"expires": 0.0, "value": {}}
_CACHE_TTL_SECONDS = 3.0


def load_feature_defaults(db: Session | None = None) -> dict[str, Any]:
    """Read the shared feature defaults with a short process-local cache."""
    if db is not None:
        # Admission already owns a connection. Do not wait on the cache lock:
        # its refresher may itself be waiting for this connection pool.
        row = db.get(SystemConfig, "application.features")
        return row.value if row and isinstance(row.value, dict) else {}
    now = time.monotonic()
    with _lock:
        if now < _cache["expires"]:
            return _cache["value"]
        try:
            with SessionLocal() as db:
                row = db.get(SystemConfig, "application.features")
                value = row.value if row and isinstance(row.value, dict) else {}
        except Exception:
            value = {}
        _cache.update(value=value, expires=now + _CACHE_TTL_SECONDS)
        return value


def update_feature_defaults_cache(value: dict[str, Any]) -> None:
    """Publish an administrator save immediately to this process's readers."""
    with _lock:
        _cache.update(value=value, expires=time.monotonic() + _CACHE_TTL_SECONDS)


def parse_worker_concurrency(default: int = 4, maximum: int = 32, *, db: Session | None = None) -> int:
    """Return the shared LLM request limit (historical parse setting key)."""
    generation = load_feature_defaults(db).get("generation", {})
    value = generation.get("parse_worker_concurrency", default) if isinstance(generation, dict) else default
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


# core.config merges administrator feature defaults on every read; it must not
# import platform (S3 direction), so this module registers its loader instead.
# Both entry points (main/worker) import this module before first use.
set_platform_defaults_provider(load_feature_defaults)


def client_logs_enabled(db: Session) -> bool:
    """One platform-wide display switch; legacy workspace preferences cannot enable it."""
    row = db.get(SystemConfig, "client.logs")
    return bool(row and isinstance(row.value, dict) and row.value.get("enabled") is True)


PROJECT_RETENTION_KEY = "retention.projects"
DEFAULT_PROJECT_TTL_DAYS = 30
DEFAULT_TRASH_DAYS = 7
MAX_RETENTION_DAYS = 3650


def normalize_project_retention(value: Any) -> dict[str, int]:
    """Clamp a stored/submitted retention record to its valid range.

    ``project_ttl_days`` is a live project's lifetime from creation (0 keeps
    projects forever); ``trash_days`` is how long a trashed project stays
    recoverable (>= 1)."""
    raw = value if isinstance(value, dict) else {}

    def number(key: str, default: int, low: int) -> int:
        try:
            return max(low, min(MAX_RETENTION_DAYS, int(raw.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {"project_ttl_days": number("project_ttl_days", DEFAULT_PROJECT_TTL_DAYS, 0), "trash_days": number("trash_days", DEFAULT_TRASH_DAYS, 1)}


def project_retention(db: Session) -> dict[str, int]:
    row = db.get(SystemConfig, PROJECT_RETENTION_KEY)
    return normalize_project_retention(row.value if row is not None else None)


RETENTION_STARTED_KEY = "retention.started_at"


def retention_started_at(db: Session):
    """When project expiry first became active (None before the first daily pass).

    Existing projects and trash entries are aged from no earlier than this moment, so
    switching the feature on never deletes data that was already past the default
    deadlines; explicit administrator changes to the day counts still apply at once."""
    from datetime import datetime, timezone

    row = db.get(SystemConfig, RETENTION_STARTED_KEY)
    try:
        value = datetime.fromisoformat(row.value["at"]) if row is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def ensure_retention_started(db: Session):
    started = retention_started_at(db)
    if started is None:
        from datetime import datetime, timezone

        started = datetime.now(timezone.utc)
        row = db.get(SystemConfig, RETENTION_STARTED_KEY)
        if row is None:
            db.add(SystemConfig(key=RETENTION_STARTED_KEY, value={"at": started.isoformat()}))
        else:
            row.value = {"at": started.isoformat()}
        db.commit()
    return started
