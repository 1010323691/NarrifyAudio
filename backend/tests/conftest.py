"""Pytest path setup.

Ensures the project root is on ``sys.path`` so ``import backend.engines.book``
works no matter which directory pytest is launched from. (This is the Python
analogue of how ``engine.test.js`` loads the engine — now a plain import.)
"""
import os
import sys
from pathlib import Path

# backend/tests/conftest.py -> parents[2] == project root (Narrify Audio/)
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Application deployments use PostgreSQL. Tests opt into a disposable local
# SQLite database explicitly so the suite remains runnable without services.
os.environ.setdefault("NARRIFY_DATABASE_URL", "sqlite://")
os.environ.setdefault("NARRIFY_AUTO_CREATE_SCHEMA", "true")
