"""Cached access to administrator-managed application feature defaults."""
from __future__ import annotations

import threading
import time
from typing import Any

from .database import SessionLocal
from .models import SystemConfig


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
