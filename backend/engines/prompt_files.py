"""Shared loader for the bundled ``backend/resources/*prompts.txt`` files.

Each file holds a ``(system_prompt, user_prompt_template)`` pair separated by a single
``---SEPARATOR---`` line. Results are cached per path and invalidated by file mtime, so
edits are picked up without restarting the app. ``script_prompts`` (解析),
``check_prompts`` (重判) and ``persona_prompts`` (音色设计) each keep their own file and
public ``load_*`` function and delegate here.
"""
from __future__ import annotations

import os
from pathlib import Path

SEPARATOR = "---SEPARATOR---"

_cache: dict[Path, tuple[float, tuple[str, str]]] = {}


def load_separated_prompts(path: Path, *, purpose: str) -> tuple[str, str]:
    """Read ``path`` → ``(system_prompt, user_prompt_template)``.

    ``purpose`` is only used in the error messages (what the file is needed for).
    """
    if not path.exists():
        raise RuntimeError(f"{path.name} not found at {path}. This file is required for {purpose}.")

    mtime = os.path.getmtime(path)
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Error reading {path.name}: {e}")

    parts = raw.split(SEPARATOR, maxsplit=1)
    if len(parts) != 2:
        raise RuntimeError(
            f"{path.name} is malformed: expected exactly one '{SEPARATOR}' delimiter."
        )

    prompts = (parts[0].strip(), parts[1].strip())
    _cache[path] = (mtime, prompts)
    return prompts
