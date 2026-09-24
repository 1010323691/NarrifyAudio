"""Bounded dispatch for the remaining in-process batch task adapters."""
from __future__ import annotations

import time

from ..core.tasks import TERMINAL, TaskStatus, get_task_manager


PREFETCH_DEPTH = 4


def run_bounded_task_coordinator(ordered: list[str], gate_fn) -> None:
    """Start pending shells in order without overfilling the shared work gate."""
    manager = get_task_manager()
    while True:
        tasks = [manager.get(task_id) for task_id in ordered]
        if all(task is None or task.status in TERMINAL for task in tasks):
            return
        running = sum(
            task is not None and task.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
            for task in tasks
        )
        waiting = max(0, running - gate_fn().active)
        while waiting < PREFETCH_DEPTH:
            next_id = next(
                (task_id for task_id, task in zip(ordered, tasks)
                 if task is not None and task.status is TaskStatus.PENDING),
                None,
            )
            if next_id is None:
                break
            try:
                manager.start(next_id)
            except (ValueError, KeyError):
                break
            tasks = [manager.get(task_id) for task_id in ordered]
            running = sum(
                task is not None and task.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
                for task in tasks
            )
            waiting = max(0, running - gate_fn().active)
        time.sleep(0.2)
