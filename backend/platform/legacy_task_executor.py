"""Compatibility import for the durable engine task adapter."""
from __future__ import annotations

from .engine_task_executor import execute_engine_task

execute_legacy_engine = execute_engine_task

__all__ = ["execute_engine_task", "execute_legacy_engine"]
