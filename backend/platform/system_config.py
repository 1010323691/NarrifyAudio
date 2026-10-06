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


def load_feature_defaults() -> dict[str, Any]:
    """Read the shared feature defaults with a short process-local cache."""
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


def parse_worker_concurrency(default: int = 4, maximum: int = 32) -> int:
    """Return the shared LLM request limit (historical parse setting key)."""
    generation = load_feature_defaults().get("generation", {})
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
