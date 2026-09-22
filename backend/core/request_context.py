"""Request-scoped workspace binding for the multi-user compatibility layer."""
from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path


_UNSET = object()
_workspace: ContextVar[Path | None | object] = ContextVar("narrify_workspace", default=_UNSET)


def bind_workspace(path: Path | None) -> Token:
    return _workspace.set(path.resolve() if path is not None else None)


def reset_workspace(token: Token) -> None:
    _workspace.reset(token)


def bound_workspace() -> Path | None | object:
    """Return the request workspace, or a sentinel for non-request callers."""
    return _workspace.get()
