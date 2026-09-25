"""Unified logging: a rotating file (``<workspace>/logs/app.log``) plus console.

Pointed at once from the app lifespan: the file handler targets the workspace's
``logs/app.log`` and is absent (console-only) while no workspace is set — the
app never writes logs into the project directory. The root-logger level is
taken from the config so it can be changed at runtime; routine
``uvicorn.access`` lines are demoted to keep the log readable. (Durable task
activity is reported through the task platform's TaskEvent log; this setup
serves the standard ``logging`` used by everything else.)
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_console_installed = False
_file_handler: RotatingFileHandler | None = None
_file_path: str | None = None
_proactor_filter_installed = False

_FMT = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")


class _ExpectedProactorDisconnectFilter(logging.Filter):
    """Hide the benign Windows Proactor client-disconnect traceback.

    When a browser closes an SSE/HTTP connection, Windows may report the peer
    reset as WinError 10054 while asyncio is doing transport cleanup.  The
    application cannot recover anything from this callback and the request has
    already ended, so logging it as an ERROR only creates noise.  Keep the
    filter narrowly scoped to the asyncio logger and this one error code so
    genuine asyncio failures remain visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "asyncio" or not record.exc_info:
            return True
        exc = record.exc_info[1]
        return not (
            isinstance(exc, ConnectionResetError)
            and getattr(exc, "winerror", None) == 10054
        )


def setup_logging(logs_dir: Path | None, level: str = "INFO") -> None:
    """Point logging at ``logs_dir/app.log`` (or console-only when ``logs_dir`` is
    ``None``). Safe to call repeatedly: only the file handler is re-targeted, the
    console handler is kept, and a level change is applied to the root logger in
    place.

    A ``logs_dir`` whose parent (the workspace folder) no longer exists — a stale
    pointer — degrades to console-only instead of resurrecting a ghost ``logs/``
    tree at the old location.
    """
    global _console_installed, _file_handler, _file_path, _proactor_filter_installed
    if logs_dir is not None and not logs_dir.parent.exists():
        logs_dir = None
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not _console_installed:
        console = logging.StreamHandler()
        console.setFormatter(_FMT)
        root.addHandler(console)
        # Keep routine request access lines out of the log.
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        _console_installed = True

    if not _proactor_filter_installed:
        logging.getLogger("asyncio").addFilter(_ExpectedProactorDisconnectFilter())
        _proactor_filter_installed = True

    target = str(logs_dir / "app.log") if logs_dir is not None else None
    if target == _file_path:
        return  # already pointed here
    if _file_handler is not None:
        root.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
    _file_path = target
    if target is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(_FMT)
        root.addHandler(handler)
        _file_handler = handler
