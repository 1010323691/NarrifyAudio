"""Shared data contracts for durable task execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifact_publication import PublicationJournal, PublicationJournalBundle


class TaskExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TaskCancelledError(TaskExecutionError):
    def __init__(self) -> None:
        super().__init__("cancelled", "任务已取消", retryable=False)


@dataclass(frozen=True)
class TaskClaim:
    task_id: str
    attempt_id: str
    attempt_no: int
    lease_token: str
    worker_id: str
    owner_id: str
    project_id: str
    task_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class TaskFileOutcome:
    temp_path: Path
    output_name: str
    content_type: str
    size_bytes: int
    sha256: str
    publish_module: str | None = None


@dataclass(frozen=True)
class TaskSideEffectOutput:
    temp_path: Path
    final_path: Path


@dataclass(frozen=True)
class TaskOutcome:
    temp_path: Path
    output_name: str
    content_type: str
    size_bytes: int
    sha256: str
    metadata: dict[str, Any]
    publish_module: str | None = None
    additional_outputs: tuple[TaskFileOutcome, ...] = field(default_factory=tuple)
    side_effect_outputs: tuple[TaskSideEffectOutput, ...] = field(default_factory=tuple)
    side_effect_deletes: tuple[Path, ...] = field(default_factory=tuple)
    publication_journal: PublicationJournal | PublicationJournalBundle | None = None
