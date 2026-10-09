"""Versioned role-hint summaries; worker role jobs need no all-pairs algorithm."""
import hashlib
import json
import os
from pathlib import Path
import uuid

from .file_lock import exclusive_file_lock
from .role_hints import suggest_role_hints

MAX_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 32


def cached_role_hints(names, config, counts, directory):
    relevant = [[name, counts.get(name, 0), (config.get(name) or {}).get("description"),
                 (config.get(name) or {}).get("gender")] for name in names]
    version = hashlib.sha256(json.dumps([1, relevant], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Role hint cache must not follow directory symlinks")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (version + ".json")
    with exclusive_file_lock(directory / ".cache.lock"):
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BYTES:
            try:
                data = json.loads(path.read_bytes())
                if data.get("version") == version and isinstance(data.get("hints"), dict):
                    return data["hints"]
            except (OSError, ValueError):
                pass
        hints = suggest_role_hints(names, config, counts)
        payload = json.dumps({"version": version, "hints": hints}, ensure_ascii=False).encode()
        if len(payload) <= MAX_BYTES:
            pending = directory / ("." + version + "." + uuid.uuid4().hex + ".tmp")
            try:
                pending.write_bytes(payload)
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)
            files = sorted((file for file in directory.glob("*.json") if len(file.stem) == 64 and all(character in "0123456789abcdef" for character in file.stem)),
                           key=lambda file: file.stat().st_mtime_ns, reverse=True)
            total = 0
            for index, file in enumerate(files):
                total += file.stat().st_size
                if index >= MAX_ENTRIES or total > MAX_BYTES:
                    file.unlink(missing_ok=True)
        return hints
