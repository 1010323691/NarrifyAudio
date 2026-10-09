from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .platform_settings import settings


class Base(DeclarativeBase):
    pass


def database_role() -> str:
    """Detect the standard -m worker entrypoint before either engine is built."""
    main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    default = "worker" if getattr(main_spec, "name", None) == "backend.worker" else "api"
    role = os.getenv("NARRIFY_DB_ROLE", default).strip().lower()
    if role not in {"api", "worker"}:
        raise ValueError("NARRIFY_DB_ROLE must be api or worker")
    return role


def _pool_setting(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer >= {minimum}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _pool_kwargs(*, lock: bool = False) -> dict:
    role = database_role()
    prefix = f"NARRIFY_{role.upper()}_DB_{'LOCK_' if lock else ''}POOL_"
    defaults = {"api": (8, 0) if lock else (16, 0), "worker": (1, 0) if lock else (2, 4)}
    size, overflow = defaults[role]
    # Worker publications, lease renewals and dispatch share this bounded pool.
    timeout = 30 if role == "worker" and not lock else 3
    return {
        "pool_pre_ping": True,
        "pool_size": _pool_setting(prefix + "SIZE", size, minimum=1),
        "max_overflow": _pool_setting(prefix + "MAX_OVERFLOW", overflow, minimum=0),
        "pool_timeout": _pool_setting(prefix + "TIMEOUT", timeout, minimum=1),
        "pool_recycle": 1800,
    }


def _engine_kwargs() -> dict:
    if settings.database_url in {"sqlite://", "sqlite:///:memory:"}:
        return {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    if settings.database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return _pool_kwargs()


engine = create_engine(settings.database_url, future=True, **_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _lock_engine_kwargs() -> dict:
    """Pool for the migration-coordination lock sessions (see
    ``main.protect_storage_during_migration``). Those sessions stay open for the
    WHOLE request — the transaction-scoped advisory lock dies with the
    transaction — so their connection cost must NOT land on the business pool
    above: a long request (SSE task stream) would otherwise pin a business
    connection for minutes. Sized for concurrent page loads + open streams."""
    if settings.database_url in {"sqlite://", "sqlite:///:memory:"}:
        return {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    if settings.database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return _pool_kwargs(lock=True)


lock_engine = create_engine(settings.database_url, future=True, **_lock_engine_kwargs())
LockSessionLocal = sessionmaker(bind=lock_engine, autoflush=False, autocommit=False, expire_on_commit=False)


def pool_status() -> dict | None:
    """Business-pool occupancy of this process (``None`` for pool-less SQLite)."""
    pool = engine.pool
    if not all(hasattr(pool, name) for name in ("size", "checkedout", "overflow")):
        return None
    return {"size": int(pool.size()), "checked_out": int(pool.checkedout()),
            "overflow": max(0, int(pool.overflow())), "max_overflow": int(getattr(pool, "_max_overflow", 0) or 0)}


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def initialize_schema() -> None:
    if not settings.auto_create_schema:
        return
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    from . import models  # noqa: F401  (registers all model metadata)

    Base.metadata.create_all(engine)
