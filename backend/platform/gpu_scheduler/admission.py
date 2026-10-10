"""Permits cover a model call, never the whole mixed-stage business task."""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from sqlalchemy import func, select

from ..models import GPURequest, Task, TaskAttempt, new_id
from ..task_context import EngineExecutionContext
from ..system_config import parse_worker_concurrency
from ..task_contracts import TaskClaim, TaskCancelledError
from ...core.managed_process import process_identity, identity_alive
from .config import load_config, platform_llm, service_fingerprint
from .store import transaction

_claim: ContextVar[TaskClaim | None] = ContextVar("gpu_claim", default=None)
_trace = logging.getLogger("audiobook.llm_trace")

# LLM permits poll the database, but a request that just ended in THIS process wakes the
# next waiters directly instead of leaving them asleep until their backoff expires: a deep
# queue otherwise admits roughly one request per poll interval and starves the engine.
_LLM_POLL_FIRST = 0.05
_LLM_POLL_MAX = 1.0
_llm_waiters: "collections.deque[threading.Event]" = collections.deque()
_llm_waiters_lock = threading.Lock()


def _leave_llm_queue(event) -> None:
    if event is None:
        return
    with _llm_waiters_lock:
        try:
            _llm_waiters.remove(event)
        except ValueError:
            pass


def _llm_wake(count: int = 1) -> None:
    """Wake the ``count`` longest-waiting LLM permit requests of this process."""
    with _llm_waiters_lock:
        for index, event in enumerate(_llm_waiters):
            if index >= count:
                break
            event.set()


def bind_claim(claim):
    return _claim.set(claim)


def reset_claim(token):
    _claim.reset(token)


@contextmanager
def gpu_permit(service: str, handle=None):
    claim = _claim.get()
    request_id = new_id()
    with transaction() as (db, state):
        config = load_config(db)
        managed = config.enabled or state["managed"]
        if not managed and claim is None and service != "LLM":
            bypass = True
        else:
            bypass = False
            db.add(GPURequest(id=request_id, task_id=claim.task_id if claim else None,
                              attempt_id=claim.attempt_id if claim else None, service=service,
                              status="waiting", owner_pid=os.getpid(),
                              process={"owner": process_identity(os.getpid())}))
    if bypass:
        yield None
        return
    wake = None
    if service == "LLM":
        wake = threading.Event()
        with _llm_waiters_lock:
            _llm_waiters.append(wake)
    try:
        delay = _LLM_POLL_FIRST if wake is not None else 0.2
        while True:
            if wake is not None:
                wake.clear()   # before polling: a wake-up arriving mid-poll must not be lost
            if handle is not None:
                handle.check()
            elif claim is not None:
                EngineExecutionContext(claim).check()
            # Read the current admin limit with the admission session, so a
            # different process's config cache cannot admit excess requests.
            with transaction() as (db, state):
                llm_limit = parse_worker_concurrency(db=db) if service == "LLM" else 1
                request = db.get(GPURequest, request_id)
                if request is None:
                    raise TaskCancelledError()
                config = load_config(db)
                managed = config.enabled or state["managed"]
                if claim:
                    task = db.get(Task, claim.task_id)
                    attempt = db.get(TaskAttempt, claim.attempt_id)
                    if task is None or task.status not in {"running", "paused"} or attempt is None or attempt.status != "running":
                        raise TaskCancelledError()
                allowed = not managed or (state["state"] == f"{service}_ACTIVE" and state["current"] == service)
                if managed and state["service_config"] != service_fingerprint(config, db=db):
                    allowed = False
                if allowed and service == "TTS" and managed:
                    allowed = db.scalar(select(GPURequest.id).where(
                        GPURequest.service == "TTS", GPURequest.status == "running").limit(1)) is None
                free_slots = 1
                if allowed and service == "LLM":
                    active_llm = db.scalar(select(func.count()).select_from(GPURequest).where(
                        GPURequest.service == "LLM", GPURequest.status == "running"))
                    allowed = active_llm < llm_limit
                    free_slots = max(1, llm_limit - active_llm)
                # Oldest requests are admitted first; a stream of short requests cannot starve
                # them. Every free slot is filled in ONE poll round — admitting a single
                # request per round capped the engine feed at one request per poll interval.
                if allowed and managed:
                    # Paused requests must not block runnable requests behind them.
                    first = db.scalars(select(GPURequest.id).outerjoin(Task, Task.id == GPURequest.task_id).where(
                                       GPURequest.service == service, GPURequest.status == "waiting",
                                       (GPURequest.task_id.is_(None)) | (Task.status == "running"))
                                       .order_by(GPURequest.created_at, GPURequest.id).limit(free_slots)).all()
                    allowed = request_id in first
                if allowed:
                    request.status = "running"
                    if managed and service == "LLM":
                        request.process = {**request.process, "llm_runtime": state.get("llm_runtime") or platform_llm(db).model_dump()}
                    state["served"] = True
                    break
            if wake is not None:
                wake.wait(delay)
                delay = min(delay * 2, _LLM_POLL_MAX)
            else:
                time.sleep(delay)
                # Bounded backoff: every waiting permit runs this DB-locked poll cycle,
                # so a deep backlog must not turn the wait into constant 5 Hz polling.
                delay = min(delay * 2, 2.0)
        _leave_llm_queue(wake)   # admitted: stop being a wake-up target while the call runs
        yield request_id
    finally:
        _leave_llm_queue(wake)
        with transaction() as (db, _state):
            request = db.get(GPURequest, request_id)
            if request:
                if request.process.get("exit_unconfirmed") or identity_alive(request.process.get("child", {})):
                    _state.update(state="ERROR", error="TTS 子进程尚未确认退出", reason="process exit unconfirmed")
                else:
                    db.delete(request)
        if service == "LLM":
            _llm_wake(1)   # a slot just freed (or a waiter left): hand it to the next in line


def release_paused_tts(request_id):
    if not request_id:
        return
    with transaction() as (db, _state):
        request = db.get(GPURequest, request_id)
        if request:
            if identity_alive(request.process.get("child", {})):
                raise RuntimeError("暂停时 TTS 进程尚未退出")
            db.delete(request)


def llm_admitted(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        queued_at = time.monotonic()
        claim = _claim.get()
        with gpu_permit("LLM", kwargs.get("handle")) as request_id:
            admitted_at = time.monotonic()
            from ..database import SessionLocal
            from ...core.config import LLMConfig
            runtime = None
            if request_id:
                with SessionLocal() as db:
                    request = db.get(GPURequest, request_id)
                    runtime = request.process.get("llm_runtime") if request else None
            # Use the platform service snapshot bound atomically to this permit,
            # not historical task settings or edits made after admission.
            if runtime:
                llm = LLMConfig.model_validate(runtime)
                args = list(args)
                for index, (key, value) in enumerate((("base_url", llm.base_url), ("api_key", llm.api_key), ("model", llm.model_name))):
                    if index < len(args):
                        args[index] = value
                    else:
                        kwargs[key] = value
            try:
                return function(*args, **kwargs)
            finally:
                # One line per model call: time spent waiting for a permit vs. in the call itself.
                _trace.info("llm_call task=%s wait=%.3f run=%.3f", claim.task_id if claim else "-",
                            admitted_at - queued_at, time.monotonic() - admitted_at)
    return wrapped


def allowed_task_types(db=None) -> set[str]:
    from ..task_registry import TASK_TYPES
    from .store import read_state
    config = load_config(db)
    state = read_state() if db is None else None
    if state is None:
        from ..models import GPUSchedulerState
        row = db.get(GPUSchedulerState, "local")
        state = row.value if row else {}
    if not config.enabled and not state.get("managed"):
        return set(TASK_TYPES)
    matching_config = state.get("service_config") == service_fingerprint(config, db=db)
    return {name for name, spec in TASK_TYPES.items() if not spec.gpu_initial or
            (matching_config and state.get("state") == f"{spec.gpu_initial}_ACTIVE")}


def claim_allowed(task_type: str, db=None) -> bool:
    return task_type in allowed_task_types(db)
