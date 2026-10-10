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

from pathlib import Path

from .prompt_files import load_separated_prompts

# backend/engines/persona_prompts.py -> parents[1] = backend -> backend/resources/...
_PROMPTS_FILE = Path(__file__).resolve().parents[1] / "resources" / "persona_prompts.txt"


def load_persona_prompts() -> tuple[str, str]:
    """Read ``persona_prompts.txt`` → ``(system_prompt, user_prompt_template)``.

    mtime-cached (see :mod:`.prompt_files`), so edits apply without restarting the app.
    """
    return load_separated_prompts(_PROMPTS_FILE, purpose="the voice-design prompt defaults")


# Cached at import time — the fallbacks used when the config carries no custom prompts.
PERSONA_SYSTEM_PROMPT, PERSONA_USER_PROMPT = load_persona_prompts()
