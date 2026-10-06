import sys
from types import SimpleNamespace

import pytest

from backend.platform import database


@pytest.mark.parametrize("role,size,overflow,lock_size,lock_overflow", [
    ("api", 16, 8, 16, 8), ("worker", 3, 1, 1, 0),
])
def test_database_pools_have_separate_bounded_budgets(monkeypatch, role, size, overflow, lock_size, lock_overflow):
    monkeypatch.setenv("NARRIFY_DB_ROLE", role)
    for key in ("SIZE", "MAX_OVERFLOW", "TIMEOUT"):
        monkeypatch.delenv(f"NARRIFY_{role.upper()}_DB_POOL_{key}", raising=False)
        monkeypatch.delenv(f"NARRIFY_{role.upper()}_DB_LOCK_POOL_{key}", raising=False)
    pool = database._pool_kwargs()
    lock = database._pool_kwargs(lock=True)
    assert (pool["pool_size"], pool["max_overflow"]) == (size, overflow)
    assert (lock["pool_size"], lock["max_overflow"]) == (lock_size, lock_overflow)


def test_standard_worker_entrypoint_selects_worker_budget_without_changing_environment(monkeypatch):
    monkeypatch.delenv("NARRIFY_DB_ROLE", raising=False)
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", SimpleNamespace(name="backend.worker"))
    assert database.database_role() == "worker"
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", SimpleNamespace(name="uvicorn.__main__"))
    assert database.database_role() == "api"


def test_pool_overrides_are_isolated_by_role_and_lock_pool(monkeypatch):
    monkeypatch.setenv("NARRIFY_DB_ROLE", "worker")
    monkeypatch.setenv("NARRIFY_API_DB_POOL_SIZE", "64")
    monkeypatch.setenv("NARRIFY_WORKER_DB_POOL_SIZE", "3")
    monkeypatch.setenv("NARRIFY_WORKER_DB_POOL_MAX_OVERFLOW", "0")
    monkeypatch.setenv("NARRIFY_WORKER_DB_POOL_TIMEOUT", "7")
    monkeypatch.setenv("NARRIFY_WORKER_DB_LOCK_POOL_SIZE", "2")
    assert database._pool_kwargs()["pool_size"] == 3
    assert database._pool_kwargs()["max_overflow"] == 0
    assert database._pool_kwargs()["pool_timeout"] == 7
    assert database._pool_kwargs(lock=True)["pool_size"] == 2


@pytest.mark.parametrize("suffix,value", [("SIZE", "0"), ("MAX_OVERFLOW", "-1"), ("TIMEOUT", "0"), ("SIZE", "invalid")])
def test_invalid_pool_settings_are_rejected(monkeypatch, suffix, value):
    monkeypatch.setenv("NARRIFY_DB_ROLE", "worker")
    monkeypatch.setenv(f"NARRIFY_WORKER_DB_POOL_{suffix}", value)
    with pytest.raises(ValueError, match=f"NARRIFY_WORKER_DB_POOL_{suffix}"):
        database._pool_kwargs()


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:", "sqlite:///test.db"])
def test_sqlite_test_pools_are_unchanged(monkeypatch, url):
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_url=url))
    monkeypatch.setenv("NARRIFY_WORKER_DB_POOL_SIZE", "0")
    assert "pool_size" not in database._engine_kwargs()
    assert "pool_size" not in database._lock_engine_kwargs()
