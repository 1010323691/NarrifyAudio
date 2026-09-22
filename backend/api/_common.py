"""Shared helpers for the API routers."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from ..core import pathio
from ..core.paths import get_layout
from ..engines.book import decode_buffer


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
    layout = get_layout()
    if layout.workspace is not None and not layout.workspace.exists():
        raise HTTPException(
            409,
            f"工作目录不存在（{layout.workspace}）——目录可能已被移动或删除，"
            f"请在「开始」页重新选择工作目录。",
        )


def resolve_inbound_path(path: str, *, label: str = "文件") -> Path:
    """Resolve a client-provided file path against the current workspace.

    Accepts workspace-relative and absolute paths alike; a stale absolute path
    (the workspace folder was moved) is recovered from its original directory
    structure or by file name when possible. Anything unresolvable raises a
    clear 400 — never a silent miss. With no workspace set it degrades to the
    legacy raw behaviour (plain existence check).
    """
    v = (path or "").strip()
    if not v:
        raise HTTPException(400, "未提供路径。")
    ws = get_layout().workspace
    if ws is not None:
        try:
            return pathio.resolve_path(v, ws, strict=True, label=label)
        except pathio.PathOutsideWorkspace as e:
            raise HTTPException(400, str(e))
        except pathio.PathNotFoundError as e:
            raise HTTPException(400, str(e))
    p = Path(v)
    if not p.exists():
        raise HTTPException(400, f"文件不存在：{path}")
    return p


def read_decoded_file(path: str) -> tuple[str, str, Path]:
    """Read the file at ``path`` and auto-detect its encoding.

    The frontend is a thin client: it hands the backend a file path (the file
    is uploaded into ``01_input/`` first); the backend does the reading /
    decoding. The path resolves against the current workspace (a stale absolute
    path from a moved workspace is recovered, see ``resolve_inbound_path``).
    Returns ``(text, encoding_label, resolved_path)``.
    """
    p = resolve_inbound_path(path, label="文件")
    if not p.is_file():
        raise HTTPException(400, f"不是一个文件：{path}")
    data = p.read_bytes()
    if not data:
        raise HTTPException(400, "文件为空。")
    try:
        text, enc = decode_buffer(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return text, enc, p


def partial_copy(model, overrides: dict):
    """Return ``model`` with only the *known* keys in ``overrides`` applied, so a
    stray/unknown key from the client can't crash the request."""
    valid = set(model.model_fields.keys())
    clean = {k: v for k, v in (overrides or {}).items() if k in valid}
    return model.model_copy(update=clean) if clean else model
