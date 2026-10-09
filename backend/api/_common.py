"""Shared helpers for the API routers."""
from __future__ import annotations

from fastapi import HTTPException

from ..core.paths import get_or_prepare_layout


def require_workspace() -> None:
    """Guard for endpoints that write pipeline artifacts.

    A run without a chosen workspace would scatter files into the project
    directory, so the pipeline stays locked until the user sets one on the
    dashboard (开始). Read-only endpoints do not call this. A pointer that
    names a folder which no longer exists (the workspace was moved or deleted)
    is reported clearly — the user re-selects it on the dashboard.
    """
    from ..core.paths import is_workspace_set

    if not is_workspace_set():
        raise HTTPException(409, "尚未设置工作空间——请先在「开始」页选择文件夹。")
    layout = get_or_prepare_layout()
    if layout.workspace is not None and not layout.workspace.exists():
        raise HTTPException(
            409,
            f"工作目录不存在（{layout.workspace}）——目录可能已被移动或删除，"
            f"请在「开始」页重新选择工作目录。",
        )


def partial_copy(model, overrides: dict):
    """Return ``model`` with only the *known* keys in ``overrides`` applied, so a
    stray/unknown key from the client can't crash the request."""
    valid = set(model.model_fields.keys())
    clean = {k: v for k, v in (overrides or {}).items() if k in valid}
    return model.model_copy(update=clean) if clean else model
