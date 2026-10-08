"""Offline BGM migration/export. Stop all API/Worker writers before using this CLI."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.engines.bgm_storage import ensure_migrated, export_legacy, STATE_DIR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--export-legacy", type=Path, help="Separate downgrade export directory")
    args = parser.parse_args()
    if not args.workspace.is_dir() or args.workspace.is_symlink():
        parser.error("workspace must be an existing regular directory")
    layout = SimpleNamespace(bgm=args.workspace / "08_bgm")
    if args.export_legacy:
        export_legacy(layout, args.export_legacy)
        print(json.dumps({"exported": str(args.export_legacy)}, ensure_ascii=False))
    else:
        ensure_migrated(layout)
        print((layout.bgm / STATE_DIR / "migration-complete.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
