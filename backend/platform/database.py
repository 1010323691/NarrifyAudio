from __future__ import annotations

from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .platform_settings import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    if settings.database_url in {"sqlite://", "sqlite:///:memory:"}:
        return {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    if settings.database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    # Sized for a page load firing 10+ concurrent requests, each of which
    # checks out up to two connections (middleware + endpoint session).
    # pool_recycle keeps long-idle connections from outliving server-side
    # session timeouts.
    return {
        "pool_pre_ping": True,
        "pool_size": 20,
        "max_overflow": 20,
        "pool_timeout": 60,
        "pool_recycle": 1800,
    }


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
    return {
        "pool_pre_ping": True,
        "pool_size": 32,
        "max_overflow": 16,
        "pool_timeout": 30,
        "pool_recycle": 1800,
    }


lock_engine = create_engine(settings.database_url, future=True, **_lock_engine_kwargs())
LockSessionLocal = sessionmaker(bind=lock_engine, autoflush=False, autocommit=False, expire_on_commit=False)


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
