"""Permits cover a model call, never the whole mixed-stage business task."""
from __future__ import annotations

import os
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
    try:
        delay = 0.2
        while True:
            if handle is not None:
                handle.check()
            elif claim is not None:
                EngineExecutionContext(claim).check()
            # Read the shared admin limit before taking the admission lock;
            # the accessor may open a DB session when its cache expires.
            llm_limit = parse_worker_concurrency() if service == "LLM" else 1
            with transaction() as (db, state):
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
                if allowed and service == "LLM":
                    active_llm = db.scalar(select(func.count()).select_from(GPURequest).where(
                        GPURequest.service == "LLM", GPURequest.status == "running"))
                    allowed = active_llm < llm_limit
                # Oldest request is admitted first; a stream of short requests cannot starve it.
                if allowed and managed:
                    # Paused requests must not block runnable requests behind them.
                    first = db.scalar(select(GPURequest.id).outerjoin(Task, Task.id == GPURequest.task_id).where(
                                      GPURequest.service == service, GPURequest.status == "waiting",
                                      (GPURequest.task_id.is_(None)) | (Task.status == "running"))
                                      .order_by(GPURequest.created_at, GPURequest.id).limit(1))
                    allowed = first == request_id
                if allowed:
                    request.status = "running"
                    if managed and service == "LLM":
                        request.process = {**request.process, "llm_runtime": state.get("llm_runtime") or platform_llm(db).model_dump()}
                    state["served"] = True
                    break
            time.sleep(delay)
            # Bounded backoff: every waiting permit runs this DB-locked poll cycle,
            # so a deep backlog must not turn the wait into constant 5 Hz polling.
            delay = min(delay * 2, 2.0)
        yield request_id
    finally:
        with transaction() as (db, _state):
            request = db.get(GPURequest, request_id)
            if request:
                if request.process.get("exit_unconfirmed") or identity_alive(request.process.get("child", {})):
                    _state.update(state="ERROR", error="TTS 子进程尚未确认退出", reason="process exit unconfirmed")
                else:
                    db.delete(request)


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
        with gpu_permit("LLM", kwargs.get("handle")) as request_id:
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
            return function(*args, **kwargs)
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
