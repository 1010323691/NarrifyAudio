"""Safe metadata-only traversal helpers for managed project directories."""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path


def iter_regular_project_files(root: Path) -> Iterator[tuple[Path, Path, os.stat_result]]:
    """Yield regular files and root-relative paths without following symlinks."""
    if root.is_symlink() or not root.is_dir():
        return
    resolved_root = root.resolve()
    for directory, child_dirs, filenames in os.walk(root, topdown=True, followlinks=False):
        parent = Path(directory)
        child_dirs[:] = [name for name in child_dirs if not (parent / name).is_symlink()]
        for filename in filenames:
            path = parent / filename
            if path.is_symlink():
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(resolved_root):
                    continue
                stat = path.stat()
                relative = path.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                continue
            if not path.is_file():
                continue
            yield path, relative, stat
