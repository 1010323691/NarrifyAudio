from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..core.paths import PROJECT_ROOT


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PlatformSettings:
    database_url: str = os.getenv(
        "NARRIFY_DATABASE_URL",
        "postgresql+psycopg://narrify:change-me@localhost:5432/narrify",
    )
    storage_root: Path = Path(
        os.getenv("NARRIFY_STORAGE_ROOT", str(PROJECT_ROOT / "storage"))
    ).resolve()
    session_cookie: str = os.getenv("NARRIFY_SESSION_COOKIE", "narrify_session")
    csrf_cookie: str = os.getenv("NARRIFY_CSRF_COOKIE", "narrify_csrf")
    session_ttl_hours: int = int(os.getenv("NARRIFY_SESSION_TTL_HOURS", "24"))
    cookie_secure: bool = _bool_env("NARRIFY_COOKIE_SECURE", False)
    registration_enabled: bool = _bool_env("NARRIFY_REGISTRATION_ENABLED", True)
    # Production schema changes must go through Alembic. Tests and explicitly
    # configured local SQLite runs may opt into create_all.
    auto_create_schema: bool = _bool_env("NARRIFY_AUTO_CREATE_SCHEMA", False)
    max_upload_bytes: int = int(
        os.getenv("NARRIFY_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))
    )
    task_lease_seconds: int = int(os.getenv("NARRIFY_TASK_LEASE_SECONDS", "120"))
    task_max_attempts: int = int(os.getenv("NARRIFY_TASK_MAX_ATTEMPTS", "3"))
    bootstrap_admin_email: str = os.getenv("NARRIFY_BOOTSTRAP_ADMIN_EMAIL", "")
    bootstrap_admin_password: str = os.getenv("NARRIFY_BOOTSTRAP_ADMIN_PASSWORD", "")
    initial_quota_units: int = int(os.getenv("NARRIFY_INITIAL_QUOTA_UNITS", "0"))


settings = PlatformSettings()
