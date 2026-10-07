"""Isolated production-worker entry point for tts_batch_benchmark.py."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def load_worker():
    spec = importlib.util.spec_from_file_location("tts_benchmark_worker", ROOT / "tts-engine/tts_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe-max", type=int, required=True)
    parser.add_argument("--resident-types", default="custom,clone")
    options, remaining = parser.parse_known_args()
    needed = set(options.resident_types.split(","))
    if not needed or not needed <= {"custom", "clone", "design"} or options.probe_max < 1:
        parser.error("invalid resident types or probe ceiling")
    worker = load_worker()
    # This child alone may test above the production ceiling. It does not change
    # source, model generation, watchdogs, or any running production process.
    worker.AUTO_BATCH_MAX = options.probe_max
    worker._needed_types = lambda segments, config: needed
    sys.argv = [sys.argv[0], *remaining]
    return worker.main()


if __name__ == "__main__":
    sys.exit(main())
