"""Shared controls for durable entries executed by one model subprocess."""
from __future__ import annotations

import time
from sqlalchemy import select

from .database import SessionLocal
from .models import Task
from .task_context import EngineExecutionContext
from .task_contracts import TaskCancelledError


class PooledMemberContext(EngineExecutionContext):
    """A parked member must also stop when its shared execution owner stops."""

    def __init__(self, claim, primary):
        super().__init__(claim)
        self.primary = primary

    @property
    def cancelled(self):
        return self.primary.cancelled or super().cancelled


class PooledTaskContext(EngineExecutionContext):
    def __init__(self, claims, finish, cancel, entry_key):
        super().__init__(claims[0])
        self.entry_key = entry_key
        self.contexts = {entry_key(claim): PooledMemberContext(claim, self) for claim in claims}
        self.completed = set()
        self.results = {}
        self.output_paths = {}
        self.finish = finish
        self.cancel = cancel
        self.last_control_check = 0.0

    def _check_members(self, on_pause=None):
        # Check controls in one query, rather than querying every entry on every
        # subprocess line. A settlement separately checks its own lease fence.
        if time.monotonic() - self.last_control_check < 0.5:
            return
        self.last_control_check = time.monotonic()
        active = {ctx.claim.task_id: (entry, ctx) for entry, ctx in self.contexts.items()
                  if entry not in self.completed and ctx.claim.task_id != self.claim.task_id}
        if not active:
            return
        with SessionLocal() as db:
            statuses = db.execute(select(Task.id, Task.status).where(Task.id.in_(active))).all()
        for task_id, status in statuses:
            entry, ctx = active[task_id]
            if status in {"cancelling", "cancelled"}:
                self.entry_cancelled(entry)
            elif status in {"paused", "queued"}:
                if on_pause is None:
                    ctx.check()
                else:
                    ctx.check_interruptible(on_pause)

    def check(self):
        super().check()
        self._check_members()

    def check_interruptible(self, on_pause):
        super().check_interruptible(on_pause)
        self._check_members(on_pause)

    def progress(self, fraction, current=""):
        # A finished coordinator entry can remain leased until the shared child
        # exits. Keep its own completion badge stable during that interval.
        entry = self.entry_key(self.claim)
        if entry not in self.results:
            super().progress(fraction, current)

    def entry_cancelled(self, entry):
        if self.cancelled:
            raise TaskCancelledError()
        if entry in self.completed:
            return True
        ctx = self.contexts[entry]
        if ctx.cancelled:
            if ctx.claim.task_id == self.claim.task_id:
                raise TaskCancelledError()
            self.cancel(ctx.claim)
            self.completed.add(entry)
            return True
        return False

