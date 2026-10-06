"""Test-only launcher: swap model executable resolution, keep real execution."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ['NARRIFY_DB_ROLE'] = 'worker'
from backend.core import paths
paths.MUSIC_LIBRARY_DIR = Path(os.environ['NARRIFY_TEST_MUSIC_LIBRARY'])
from backend.engines import tts_batch, voices, merge

def fixture_engine():
    return Path(sys.executable), Path(__file__).with_name('tts_worker.py')

for module in (tts_batch, voices, merge):
    module.resolve_engine = fixture_engine
from backend.worker import main
main()
