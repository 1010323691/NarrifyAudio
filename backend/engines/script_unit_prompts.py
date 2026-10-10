"""Default prompts for the numbered-unit parse protocol (``parse_protocol == "units"``).

Reads the bundled ``backend/resources/default_unit_prompts.txt`` and splits it on
``---SEPARATOR---`` into a ``(system_prompt, user_prompt_template)`` pair; the user
template carries ``{context}`` and ``{units}`` placeholders. Config-supplied
``prompts.unit_system_prompt`` / ``prompts.unit_user_prompt`` override these defaults when
non-empty, exactly like the legacy JSON-protocol prompts in :mod:`.script_prompts`.
"""
from __future__ import annotations

from pathlib import Path

from .prompt_files import load_separated_prompts

_PROMPTS_FILE = Path(__file__).resolve().parents[1] / "resources" / "default_unit_prompts.txt"


def load_default_unit_prompts() -> tuple[str, str]:
    """Read ``default_unit_prompts.txt`` → ``(system_prompt, user_prompt_template)``
    (mtime-cached, so edits apply without a restart)."""
    return load_separated_prompts(_PROMPTS_FILE, purpose="unit-protocol LLM prompt defaults")


DEFAULT_UNIT_SYSTEM_PROMPT, DEFAULT_UNIT_USER_PROMPT = load_default_unit_prompts()
