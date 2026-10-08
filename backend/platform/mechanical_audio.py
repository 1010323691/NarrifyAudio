"""Fenced host-wide CPU audio permits; a lease expiry is not proof of child exit."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from contextvars import ContextVar

from sqlalchemy import func, select
import psutil

from ..core.concurrency import merge_concurrency_limit
from ..core.managed_process import identity_alive, process_identity, spawn_owned, terminate_owned, close_owned, tree_exited, windows_job_alive
from .database import SessionLocal
from .gpu_scheduler.store import host_lock
from .models import MechanicalAudioPermit, MechanicalAudioState, Task, TaskAttempt
from .task_contracts import TaskExecutionError
from .tts_resource_budget import memory_snapshot

AUDIO_TASK_TYPES = ("tts.merge", "bgm.mix")
_claim = ContextVar("mechanical_audio_claim", default=None)
GIB = 1024 ** 3


def bind_claim(claim):
    return _claim.set(claim if claim.task_type in AUDIO_TASK_TYPES else None)


def reset_claim(token):
    _claim.reset(token)


def has_bound_claim():
    return _claim.get() is not None


def cancel_registered(proc):
    if has_bound_claim():
        terminate_owned(proc)
    elif proc.poll() is None:
        proc.kill()


def _state(db):
    state = db.get(MechanicalAudioState, "local")
    if state is None:
        state = MechanicalAudioState(id="local", memory_paused=False, error="")
        db.add(state)
        db.flush()
    return state


def _children_alive(process):
    for child in process.get("children", []):
        if child.get("finished"):
            continue
        if identity_alive(child):
            return True
        if os.name == "nt":
            if not child.get("job") or windows_job_alive(child["job"]):
                return True
            continue
        for item in psutil.process_iter():
            try:
                if os.getpgid(item.pid) == child["pid"] and item.status() != psutil.STATUS_ZOMBIE:
                    return True
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
    return False


def _reap(db, state):
    unresolved = False
    for permit in db.scalars(select(MechanicalAudioPermit)):
        try:
            owner_alive = identity_alive(permit.process.get("owner", {}))
            children_alive = _children_alive(permit.process)
            attempt = db.get(TaskAttempt, permit.attempt_id)
            task = db.get(Task, permit.task_id)
            terminal = not attempt or attempt.status != "running" or not task or task.status in {"succeeded", "failed", "cancelled", "timeout"}
            if not children_alive and not permit.process.get("spawning") and (not owner_alive or terminal):
                db.delete(permit)
            elif not owner_alive:
                unresolved = True
        except Exception:
            # AccessDenied/unknown process state must never free an occupied slot.
            unresolved = True
    state.error = "音频子进程退出尚未确认，禁止接纳新的音频任务" if unresolved else ""
    db.flush()


def _limit(db, state):
    available, _ = memory_snapshot()
    if available is not None:
        if available < 2 * GIB:
            state.memory_paused = True
        elif available >= 3 * GIB:
            state.memory_paused = False
    if state.memory_paused or state.error:
        return 0
    return 1 if available is None else merge_concurrency_limit()


def capacity_available():
    # Run before callers open their task transaction: no nested pool checkout.
    with host_lock(), SessionLocal.begin() as db:
        state = _state(db)
        _reap(db, state)
        limit = _limit(db, state)
        return (db.scalar(select(func.count()).select_from(MechanicalAudioPermit)) or 0) < limit


def status(db):
    """Bounded read model for operations; never exposes process identities."""
    state = db.get(MechanicalAudioState, "local")
    available, _ = memory_snapshot()
    return {"limit": 1 if available is None else merge_concurrency_limit(),
            "active": db.scalar(select(func.count()).select_from(MechanicalAudioPermit)) or 0,
            "memory_paused": bool(state and state.memory_paused),
            "error": state.error if state else ""}


def reserve(db, claim):
    """Caller holds host_lock and commits this together with the fenced attempt."""
    if claim.task_type not in AUDIO_TASK_TYPES:
        return True
    existing = db.get(MechanicalAudioPermit, claim.attempt_id)
    if existing is not None:
        return existing.task_id == claim.task_id
    state = _state(db)
    limit = _limit(db, state)
    if (db.scalar(select(func.count()).select_from(MechanicalAudioPermit)) or 0) >= limit:
        return False
    owner = process_identity(os.getpid())
    if not owner:
        raise TaskExecutionError("audio_owner_unknown", "无法确认音频 Worker 进程身份")
    db.add(MechanicalAudioPermit(attempt_id=claim.attempt_id, task_id=claim.task_id,
                                 process={"owner": owner, "children": [], "spawning": False}))
    db.flush()
    return True


def release(claim):
    if claim.task_type not in AUDIO_TASK_TYPES:
        return True
    with host_lock(), SessionLocal.begin() as db:
        permit = db.get(MechanicalAudioPermit, claim.attempt_id)
        if permit is None:
            return True
        if permit.process.get("spawning") or _children_alive(permit.process):
            _state(db).error = "音频子进程退出尚未确认，许可保留"
            return False
        db.delete(permit)
        return True


def resume(claim, cancelled):
    if claim.task_type not in AUDIO_TASK_TYPES:
        return
    delay = .2
    while not cancelled():
        if capacity_available():
            with host_lock(), SessionLocal.begin() as db:
                from .task_context import _attempt_is_current
                task, attempt = _attempt_is_current(db, claim)
                if task is None or attempt is None:
                    raise TaskExecutionError("audio_stale_attempt", "音频任务执行许可已失效")
                if task.status == "running" and reserve(db, claim):
                    return
        time.sleep(delay)
        delay = min(2., delay * 2)
    from ..core.task_control import TaskCancelled
    raise TaskCancelled()


def spawn_registered(cmd, **kwargs):
    claim = _claim.get()
    if claim is None:
        return subprocess.Popen(cmd, **kwargs)
    # Persist the launch window first. A parent crash between spawn and child
    # registration leaves a blocked permit, never an untracked reusable slot.
    with host_lock(), SessionLocal.begin() as db:
        permit = db.get(MechanicalAudioPermit, claim.attempt_id)
        if permit is None:
            raise TaskExecutionError("audio_permit_missing", "音频执行许可不存在")
        from .task_context import _attempt_is_current
        task, attempt = _attempt_is_current(db, claim)
        if task is None or attempt is None or task.status != "running":
            raise TaskExecutionError("audio_stale_attempt", "音频任务执行许可已失效")
        permit.process = {**permit.process, "spawning": True}
    proc = None
    try:
        proc = spawn_owned(cmd, **kwargs)
        child = process_identity(proc.pid)
        if child and (job := getattr(proc, "_narrify_job", None)):
            child = {**child, "job": job.name}
        # A very short child can already have exited; confirm its entire tree.
        if not child and not tree_exited(proc):
            raise RuntimeError("无法确认音频子进程身份")
        with host_lock(), SessionLocal.begin() as db:
            permit = db.get(MechanicalAudioPermit, claim.attempt_id)
            if permit is None:
                raise RuntimeError("音频执行许可在启动期间失效")
            permit.process = {**permit.process, "spawning": False,
                              "children": [*permit.process.get("children", []), *([child] if child else [])]}
        return proc
    except BaseException:
        if proc is not None:
            terminate_owned(proc)
            proc.wait(timeout=30)
            close_owned(proc)
        with host_lock(), SessionLocal.begin() as db:
            permit = db.get(MechanicalAudioPermit, claim.attempt_id)
            if permit:
                permit.process = {**permit.process, "spawning": False}
        raise


def finish_registered(proc):
    claim = _claim.get()
    if claim is None:
        return
    if not tree_exited(proc):
        raise RuntimeError("音频子进程树尚未退出，许可保留")
    close_owned(proc)
    with host_lock(), SessionLocal.begin() as db:
        permit = db.get(MechanicalAudioPermit, claim.attempt_id)
        if permit:
            permit.process = {**permit.process, "children": [
                {**child, "finished": True} if child.get("pid") == proc.pid else child
                for child in permit.process.get("children", [])]}


def release_safely(claim):
    if claim.task_type not in AUDIO_TASK_TYPES:
        return
    try:
        release(claim)
    except Exception:
        logging.getLogger(__name__).exception("音频许可清理失败，保留许可 task=%s", claim.task_id)
