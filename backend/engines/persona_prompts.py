"""Default persona (voice-design) prompts.

Reads the bundled ``backend/resources/persona_prompts.txt`` and splits it on
``---SEPARATOR---`` into a ``(system_prompt, user_prompt_template)`` pair — the same
pattern as ``script_prompts.py`` / ``check_prompts.py``. An mtime cache picks up edits
to the file without a restart. These are the *bundled* defaults: an empty value in
``config.app.persona_prompts`` falls back to the matching constant here.

``PERSONA_USER_PROMPT`` is a template with the placeholders ``{speaker}`` and
``{line_windows}`` (the character's sampled lines, each with its local context) —
substituted per character by ``backend/engines/voices.py`` via ``str.replace`` (so a
user-edited template containing other braces never breaks the fill).
"""
from __future__ import annotations

import os
from pathlib import Path

# backend/engines/persona_prompts.py -> parents[1] = backend -> backend/resources/...
_PROMPTS_FILE = Path(__file__).resolve().parents[1] / "resources" / "persona_prompts.txt"

_prompt_cache: dict = {"mtime": None, "prompts": None}


def load_persona_prompts() -> tuple[str, str]:
    """Read ``persona_prompts.txt`` → ``(system_prompt, user_prompt_template)``.

    Mirrors ``script_prompts.load_default_prompts`` with an mtime-based cache so edits
    to the file are picked up without restarting the app.
    """
    if not _PROMPTS_FILE.exists():
        raise RuntimeError(
            f"persona_prompts.txt not found at {_PROMPTS_FILE}. "
            "This file is required for the voice-design prompt defaults."
        )

    mtime = os.path.getmtime(_PROMPTS_FILE)
    if _prompt_cache["mtime"] == mtime and _prompt_cache["prompts"] is not None:
        return _prompt_cache["prompts"]

    try:
        raw = _PROMPTS_FILE.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Error reading persona_prompts.txt: {e}")

    parts = raw.split("---SEPARATOR---", maxsplit=1)
    if len(parts) != 2:
        raise RuntimeError(
            "persona_prompts.txt is malformed: expected exactly one '---SEPARATOR---' delimiter."
        )

    prompts = (parts[0].strip(), parts[1].strip())
    _prompt_cache["mtime"] = mtime
    _prompt_cache["prompts"] = prompts
    return prompts


# Cached at import time — the fallbacks used when the config carries no custom prompts.
PERSONA_SYSTEM_PROMPT, PERSONA_USER_PROMPT = load_persona_prompts()
