"""Request-scoped workspace binding for the multi-user compatibility layer."""
from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path


class _WorkspaceUnset:
    """Sentinel held by the ContextVar outside request scope (see bound_workspace)."""

    __slots__ = ()


_UNSET = _WorkspaceUnset()
_workspace: ContextVar[Path | None | _WorkspaceUnset] = ContextVar(
    "narrify_workspace", default=_UNSET
)


def bind_workspace(path: Path | None) -> Token:
    return _workspace.set(path.resolve() if path is not None else None)


def reset_workspace(token: Token) -> None:
    _workspace.reset(token)


def bound_workspace() -> Path | None | _WorkspaceUnset:
    """The request-bound workspace; the ``_UNSET`` sentinel for non-request callers."""
    return _workspace.get()
