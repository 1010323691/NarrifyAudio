"""Pytest path setup.

Ensures the project root is on ``sys.path`` so ``import backend.engines.book``
works no matter which directory pytest is launched from. (This is the Python
analogue of how ``engine.test.js`` loads the engine — now a plain import.)
"""
import os
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

import pytest

# backend/tests/conftest.py -> parents[2] == project root (Narrify Audio/)
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Override inherited deployment settings before importing the application. A
# developer's shell must never make the suite connect to a live database or
# write under the configured production storage root.
_test_storage = TemporaryDirectory(prefix="narrify-pytest-")
# File-backed on purpose, not in-memory: the SSE task stream runs its DB units
# in anyio worker threads, and an in-memory URL pools ONE shared pysqlite
# connection (StaticPool) — two concurrent streams then drove that single
# connection from two threads and crashed with SQLITE_MISUSE ("bad parameter
# or other API misuse"). A file gives the pool a distinct connection per
# checkout, the way production (Postgres) already has one.
os.environ["NARRIFY_DATABASE_URL"] = f"sqlite:///{_test_storage.name}/pytest.db"
os.environ["NARRIFY_AUTO_CREATE_SCHEMA"] = "true"
os.environ["NARRIFY_STORAGE_ROOT"] = _test_storage.name


@pytest.fixture(scope="session", autouse=True)
def _test_schema():
    """Create the schema once per session. The in-memory database is shared by
    every test in the process, but only the ``client`` fixtures (TestClient
    lifespans) used to call ``initialize_schema`` — so a test that talks to
    SessionLocal directly (no ``client`` fixture) depended on an EARLIER test
    module having run, and any file run in isolation failed with
    ``no such table`` on its first DB query. Do it here, before everything."""
    from backend.platform.database import engine, lock_engine, initialize_schema

    initialize_schema()
    yield
    # The file-backed DB lives in the session's temp dir: release every pooled
    # (and leaked) connection or Windows cannot delete the file at cleanup.
    # The lock pool holds connections for the middleware's whole-request lock
    # sessions, so it must be drained too.
    engine.dispose(close=True)
    lock_engine.dispose(close=True)


@pytest.fixture(autouse=True)
def _fresh_layout_cache():
    """Reset the layout memo around every test. ``core.paths`` caches
    ``get_or_prepare_layout()`` on the root ``app.json``'s ``(mtime_ns, size)``
    — but sandboxed tests point ``TEMPLATE_FILE`` at a DIFFERENT per-test
    file, and two such files can share that timestamp pair, so a test could
    silently resolve its PREDECESSOR's workspace from the cache (observed:
    test_merge reads the previous test's empty manifest). test_paths /
    test_tts_batch reset it in their own fixtures; do it here so every test
    starts and ends clean no matter which file runs."""
    from backend.core import paths as core_paths

    core_paths.reset_layout_cache()
    yield
    core_paths.reset_layout_cache()
