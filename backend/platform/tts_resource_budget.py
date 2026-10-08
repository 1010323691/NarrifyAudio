"""Preparation budgets, independent of model/GPU memory allocation."""
from __future__ import annotations

import os
import time
from pathlib import Path

from .task_contracts import TaskExecutionError

MIB = 1024 * 1024
MAX_FILE_BYTES = 8 * MIB
MAX_INPUT_BYTES = 64 * MIB
MAX_MANIFEST_BYTES = 64 * MIB
MAX_SEGMENTS = 50000
PREPARATION_MEMORY_BYTES = 1024 * MIB


def memory_snapshot():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Memory(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [(n, ctypes.c_ulonglong) for n in ("total", "available", "page", "page_available", "virtual", "virtual_available", "extended")]
        m = Memory(); m.length = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        class ProcessMemory(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in ("peak", "rss", "paged_peak", "paged", "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak")]
        p = ProcessMemory(); p.cb = ctypes.sizeof(p)
        ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(p), p.cb)
        return int(m.available), int(p.rss)
    try:
        info = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()}
        pages = int(Path('/proc/self/statm').read_text().split()[1])
        available = info['MemAvailable']
        for maximum, current in [('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.current'),
                                 ('/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory/memory.usage_in_bytes')]:
            try:
                limit = int(Path(maximum).read_text())
                used = int(Path(current).read_text())
                available = min(available, max(0, limit - used))
            except (OSError, ValueError):
                pass
        return available, pages * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, KeyError):
        return None, None


class PreparationBudget:
    def __init__(self):
        self.started = time.monotonic()
        self.input_bytes = self.manifest_bytes = self.segments = 0
        self.initial_rss = memory_snapshot()[1]

    def prepared(self):
        from ..core.observability import record_tts_stage
        record_tts_stage('preparation', (time.monotonic() - self.started) * 1000)

    def file(self, path, *, manifest=False):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        total = (self.manifest_bytes if manifest else self.input_bytes) + size
        maximum = MAX_MANIFEST_BYTES if manifest else MAX_INPUT_BYTES
        if size > MAX_FILE_BYTES or total > maximum:
            raise TaskExecutionError('tts_input_budget', '所选内容或历史清单超过安全预算，请减少选择章节')
        if manifest:
            self.manifest_bytes = total
        else:
            self.input_bytes = total
        self.check()

    def add_segments(self, count):
        self.segments += count
        if self.segments > MAX_SEGMENTS:
            raise TaskExecutionError('tts_input_budget', '一次最多准备 50000 段，请减少选择章节')
        self.check()

    def check(self):
        available, rss = memory_snapshot()
        if available is not None and available < 1024 * MIB:
            raise TaskExecutionError('tts_preparation_memory', '准备阶段可用内存不足，请稍后重试')
        if rss is not None and self.initial_rss is not None and rss - self.initial_rss > PREPARATION_MEMORY_BYTES - 64 * MIB:
            raise TaskExecutionError('tts_preparation_memory', '准备阶段超过内存预算，请减少选择章节')


_memory_paused = False

def preparation_memory_available():
    """Admission uses hysteresis so low-memory hosts do not repeatedly restart work."""
    global _memory_paused
    from .platform_settings import settings
    if not settings.tts_preparation_enabled:
        return False
    available, _ = memory_snapshot()
    if available is None:
        return True
    if available < 2 * 1024 * MIB:
        _memory_paused = True
    elif available >= 3 * 1024 * MIB:
        _memory_paused = False
    return not _memory_paused


def tts_capacity_available(db, *, exclude_task_id=None, resume_claim=None):
    """One executing pool; parked attempts retain leases without retaining the slot.

    Caller holds host_lock through any subsequent promotion/claim commit.
    """
    from sqlalchemy import select
    from .models import Task, TaskAttempt, utcnow
    if not preparation_memory_available():
        return False
    query = select(Task.id).join(TaskAttempt).where(
        Task.task_type == 'tts.batch', TaskAttempt.status == 'running',
        TaskAttempt.lease_expires_at > utcnow(),
        Task.ui_state['tts_parked'].as_boolean().is_not(True),
    )
    if exclude_task_id:
        query = query.where(Task.id != exclude_task_id)
    if resume_claim:
        task = db.get(Task, resume_claim.task_id)
        slot = (task.ui_state or {}).get('tts_slot') if task else None
        if slot:
            query = query.where(Task.ui_state['tts_slot'].as_string().is_distinct_from(slot))
        else:
            query = query.where(Task.id != resume_claim.task_id)
    return db.scalar(query.limit(1)) is None


def set_tts_parked(db, claim, parked):
    """Mark the actual claimed pool after stopping its child, under host_lock."""
    from sqlalchemy import select
    from .models import Task, TaskAttempt, utcnow
    current = db.get(Task, claim.task_id)
    slot = (current.ui_state or {}).get('tts_slot') if current else None
    query = select(Task).join(TaskAttempt).where(
        Task.owner_id == claim.owner_id, Task.project_id == claim.project_id,
        Task.task_type == 'tts.batch', TaskAttempt.status == 'running',
        TaskAttempt.lease_expires_at > utcnow(),
    )
    query = query.where(Task.ui_state['tts_slot'].as_string() == slot) if slot else query.where(Task.id == claim.task_id)
    for task in db.scalars(query.order_by(Task.id).with_for_update()):
        task.ui_state = {**(task.ui_state or {}), 'tts_parked': parked}
