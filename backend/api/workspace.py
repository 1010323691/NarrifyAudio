"""Workspace endpoints — show / change the pipeline's artifact folder.

The workspace is the user's chosen folder; every pipeline artifact, the project
config (``config/app.json``) and the log (``logs/app.log``) all live inside it
(see ``core/paths.py``). Setting it records the pointer in the root ``app.json``,
seeds the workspace's config from the root template (never overwriting an
existing one), creates any missing directories, and re-points the log file there.
Clearing it (empty path) re-locks the pipeline and drops file logging. Old files
are never moved or deleted on a change.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import config as core_config
from ..core import filesystem
from ..core import logging_setup
from ..core import workspace_history
from ..core import paths as core_paths
from ..core.paths import WORKSPACE_DIR_NAMES, get_layout

router = APIRouter(prefix="/api/workspace", tags=["workspace"])
log = logging.getLogger(__name__)


class WorkspaceRequest(BaseModel):
    path: str  # empty -> clear the workspace


class RecentWorkspaceRequest(BaseModel):
    path: str


def _info() -> dict:
    layout = get_layout()
    if layout.workspace is None:
        return {"set": False, "path": "", "exists": False, "is_default": True, "dirs": {}}
    return {
        "set": True,
        "path": str(layout.workspace),
        # The folder the pointer names may have been moved or deleted since it was set —
        # the dashboard uses this to tell the user to re-select it (the pipeline's write
        # endpoints refuse with a clear error in that state).
        "exists": layout.workspace.exists(),
        "is_default": False,
        "dirs": layout.dirs(),
    }


@router.get("")
def get_workspace() -> dict:
    """Current workspace + its artifact dirs (empty path / dirs when unset)."""
    return _info()


@router.get("/recent")
def get_recent_workspaces() -> dict:
    current = _info().get("path", "")
    # Migrate a valid pre-history pointer on first read.
    if current and Path(current).is_dir():
        try:
            existing = workspace_history.list_records(current)
            if not any(item.get("is_current") for item in existing):
                workspace_history.upsert(current)
        except (OSError, ValueError) as exc:
            log.warning("最近工作空间迁移失败：%s", exc)
    return {"workspaces": workspace_history.list_records(current)}


@router.delete("/recent")
def delete_recent_workspace(req: RecentWorkspaceRequest) -> dict:
    if not (req.path or "").strip():
        raise HTTPException(400, "历史工作空间路径不能为空")
    workspace_history.remove(req.path)
    return {"workspaces": workspace_history.list_records(_info().get("path", ""))}


@router.put("")
def set_workspace(req: WorkspaceRequest) -> dict:
    path = (req.path or "").strip()
    if not path:
        # Clear: the pipeline re-locks, logging goes console-only; nothing
        # existing is touched.
        core_config.clear_workspace()
        logging_setup.setup_logging(None, core_config.get_config().log.level)
        return _info()

    workspace = Path(path)
    if not workspace.is_absolute():
        workspace = core_paths.PROJECT_ROOT / workspace
    reason = filesystem.workspace_selection_reason(workspace)
    if reason:
        raise HTTPException(400, reason)
    # Validate by creating before persisting, so an unusable path yields a 400
    # without corrupting the stored pointer. mkdir(exist_ok) never touches
    # existing content.
    try:
        for name in (*WORKSPACE_DIR_NAMES, "logs", "config"):
            (workspace / name).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"无法创建工作空间目录：{e}")

    # Seed the workspace's own config from the root template (never overwrite an
    # existing one), then record the pointer, then re-point the log file.
    core_config.init_workspace_config(workspace)
    try:
        workspace_history.upsert(str(workspace))
    except OSError as exc:
        log.warning("最近工作空间保存失败：%s", exc)
    core_config.set_workspace_pointer(str(workspace))
    logging_setup.setup_logging(workspace / "logs", core_config.get_config().log.level)
    return _info()
