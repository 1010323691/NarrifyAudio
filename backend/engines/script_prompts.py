"""Default LLM prompts — port of the source ``app/default_prompts.py``.

Reads the bundled ``backend/resources/default_prompts.txt`` (Chinese rules and
Chinese ``instruct`` output) and splits it on ``---SEPARATOR---`` into a
``(system_prompt, user_prompt_template)`` pair. An mtime cache picks up edits to the
file without a restart. Config-supplied prompts (``config.prompts``) override these
defaults when non-empty — see ``backend/api/config.py`` / ``backend/api/script.py``.
"""
from __future__ import annotations

from pathlib import Path

from .prompt_files import load_separated_prompts

# backend/engines/script_prompts.py -> parents[1] = backend -> backend/resources/...
_PROMPTS_FILE = Path(__file__).resolve().parents[1] / "resources" / "default_prompts.txt"


def load_default_prompts() -> tuple[str, str]:
    """Read ``default_prompts.txt`` and return ``(system_prompt, user_prompt_template)``.

    mtime-cached (see :mod:`.prompt_files`), so edits apply without restarting the app.
    """
    return load_separated_prompts(_PROMPTS_FILE, purpose="LLM prompt defaults")


# Cached at import time — the fallbacks used when the config carries no custom prompts.
DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT = load_default_prompts()
