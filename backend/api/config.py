"""Config endpoints — read / write the unified persistent config (requirement #4)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core import config as core_config
from ..engines.script_prompts import load_default_prompts
from . import _common

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config() -> dict:
    """Return the full config (all sections).

    Empty ``prompts`` are seeded from the bundled defaults (mirroring the source
    ``get_config``) so the 文本解析 page can show and edit the full prompt on first
    open. This only affects the response — the stored config is left untouched.
    """
    data = core_config.get_config().model_dump()
    prompts = data.setdefault("prompts", {})
    if not prompts.get("system_prompt"):
        prompts["system_prompt"] = load_default_prompts()[0]
    if not prompts.get("user_prompt"):
        prompts["user_prompt"] = load_default_prompts()[1]
    return data


@router.put("")
def put_config(patch: dict) -> dict:
    """Merge a (possibly partial) patch into the ACTIVE (workspace) config and
    persist it there. Requires a workspace (409 otherwise) — config travels with
    the project; the root template is never written. The request body *is* the
    patch, e.g. ``{"log": {"level": "DEBUG"}, "tts": {"batch_concurrency": 6}}``.
    Returns the resulting full config.
    """
    _common.require_workspace()
    try:
        cfg = core_config.update_config(patch)
    except core_config.WorkspaceNotSetError:
        raise HTTPException(
            409, "尚未设置工作空间——配置随工程，请先在「开始」页选择文件夹。"
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean 400
        raise HTTPException(400, f"配置无效：{exc}")
    return cfg.model_dump()
