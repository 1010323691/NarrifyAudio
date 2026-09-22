"""Default LLM prompts — port of the source ``app/default_prompts.py``.

Reads the bundled ``backend/resources/default_prompts.txt`` (a verbatim copy of the
source project's file) and splits it on ``---SEPARATOR---`` into a
``(system_prompt, user_prompt_template)`` pair. An mtime cache picks up edits to the
file without a restart. Config-supplied prompts (``config.prompts``) override these
defaults when non-empty — see ``backend/api/config.py`` / ``backend/api/script.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

# backend/engines/script_prompts.py -> parents[1] = backend -> backend/resources/...
_PROMPTS_FILE = Path(__file__).resolve().parents[1] / "resources" / "default_prompts.txt"

_prompt_cache: dict = {"mtime": None, "prompts": None}


def load_default_prompts() -> tuple[str, str]:
    """Read ``default_prompts.txt`` and return ``(system_prompt, user_prompt_template)``.

    Uses an mtime-based cache to pick up edits without restarting the app, avoiding
    redundant disk reads when the file hasn't changed. (Faithful port of the source.)
    """
    if not _PROMPTS_FILE.exists():
        raise RuntimeError(
            f"default_prompts.txt not found at {_PROMPTS_FILE}. "
            "This file is required for LLM prompt defaults."
        )

    mtime = os.path.getmtime(_PROMPTS_FILE)
    if _prompt_cache["mtime"] == mtime and _prompt_cache["prompts"] is not None:
        return _prompt_cache["prompts"]

    try:
        raw = _PROMPTS_FILE.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Error reading default_prompts.txt: {e}")

    parts = raw.split("---SEPARATOR---", maxsplit=1)
    if len(parts) != 2:
        raise RuntimeError(
            "default_prompts.txt is malformed: expected exactly one '---SEPARATOR---' delimiter."
        )

    prompts = (parts[0].strip(), parts[1].strip())
    _prompt_cache["mtime"] = mtime
    _prompt_cache["prompts"] = prompts
    return prompts


# Cached at import time — the fallbacks used when the config carries no custom prompts.
DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT = load_default_prompts()
