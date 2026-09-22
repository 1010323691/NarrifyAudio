from __future__ import annotations

from sqlalchemy.orm import Session

from .config import settings
from .models import SystemConfig


def registration_enabled(db: Session) -> bool:
    config = db.get(SystemConfig, "registration.enabled")
    if config is None or not isinstance(config.value, dict):
        return settings.registration_enabled
    value = config.value.get("enabled", settings.registration_enabled)
    return bool(value)
