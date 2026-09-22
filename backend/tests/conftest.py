"""Pytest path setup.

Ensures the project root is on ``sys.path`` so ``import backend.engines.book``
works no matter which directory pytest is launched from. (This is the Python
analogue of how ``engine.test.js`` loads the engine — now a plain import.)
"""
import sys
from pathlib import Path

# backend/tests/conftest.py -> parents[2] == project root (AudiobookStudio/)
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
