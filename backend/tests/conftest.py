"""Pytest path setup.

Ensures the project root is on ``sys.path`` so ``import backend.engines.book``
works no matter which directory pytest is launched from. (This is the Python
analogue of how ``engine.test.js`` loads the engine — now a plain import.)
"""
import os
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

# backend/tests/conftest.py -> parents[2] == project root (Narrify Audio/)
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Override inherited deployment settings before importing the application. A
# developer's shell must never make the suite connect to a live database or
# write under the configured production storage root.
_test_storage = TemporaryDirectory(prefix="narrify-pytest-")
os.environ["NARRIFY_DATABASE_URL"] = "sqlite://"
os.environ["NARRIFY_AUTO_CREATE_SCHEMA"] = "true"
os.environ["NARRIFY_STORAGE_ROOT"] = _test_storage.name
