from __future__ import annotations

from sqlalchemy.orm import Session

from .config import settings
from .models import SystemConfig


def initial_quota_units(db: Session) -> int:
    config = db.get(SystemConfig, "quota.initial_units")
    if config is None or not isinstance(config.value, dict):
        return max(0, settings.initial_quota_units)
    try:
        return max(0, int(config.value.get("units", settings.initial_quota_units)))
    except (TypeError, ValueError):
        return max(0, settings.initial_quota_units)
