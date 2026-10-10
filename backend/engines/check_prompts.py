"""Default re-judgment prompts — port of the ``script_prompts`` pattern.

Reads the bundled ``backend/resources/default_check_prompts.txt`` and splits it on
``---SEPARATOR---`` into a ``(system_prompt, user_prompt_template)`` pair. The user
template carries a ``{context}`` placeholder that the re-judgment stages fill with the
per-batch context window. An mtime cache picks up edits without a restart. The in-parse
断句失败校验 / 归属抽样 stages always use these bundled defaults — the re-judgment prompts
are NOT user-configurable (the retired check stages' ``config.speaker_check`` prompt
override is gone with those stages).

This is deliberately a separate file / loader from the 解析 prompts
(``script_prompts.py`` / ``default_prompts.txt``) so the two stay fully independent.
"""
from __future__ import annotations

from pathlib import Path

from .prompt_files import load_separated_prompts

# backend/engines/check_prompts.py -> parents[1] = backend -> backend/resources/...
_PROMPTS_FILE = Path(__file__).resolve().parents[1] / "resources" / "default_check_prompts.txt"


def load_default_check_prompts() -> tuple[str, str]:
    """Read ``default_check_prompts.txt`` → ``(system_prompt, user_prompt_template)``.

    mtime-cached (see :mod:`.prompt_files`), so edits apply without restarting the app.
    """
    return load_separated_prompts(
        _PROMPTS_FILE, purpose="the in-parse re-judgment prompt defaults")


# Cached at import time — the (sole) re-judgment prompts used by both in-parse stages.
DEFAULT_CHECK_SYSTEM_PROMPT, DEFAULT_CHECK_USER_PROMPT = load_default_check_prompts()
