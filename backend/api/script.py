"""Script-generation endpoints (module: 文本解析).

``POST /generate-files`` takes the names of one or more files in the workspace's
``02_split_text/`` and starts **one independent Task per file** that runs the
``engines/script.py`` LLM → JSON pipeline (LLM / prompt / generation settings come
from the unified config; empty prompts resolve to the bundled defaults). It returns
the created ``task_ids``; the UI streams each task over SSE (per-chunk progress +
logs) and reads the resulting ``{speaker, text, instruct}`` entries from
``task.result``. Each file's JSON is written to ``03_parsed_json/<source-stem>.json``
(one file per source) and served by the shared ``GET /api/files/download/03_parsed_json/{name}``
route, so it drops straight into the TTS flow.

Dispatch is **ordered + prefetched** (not all-at-once, not strictly serial): the
endpoint pre-creates every file's task as a PENDING shell (so the response carries
all task_ids and the UI binds rows immediately), and a per-batch coordinator thread
starts them ONE BY ONE in the user's selection order, keeping at most
``PREFETCH_DEPTH`` (4) tasks waiting for a concurrency slot at any time. A task
holds its slot only during the LLM chunk-parse stage — the moment it enters the
mechanical check stages the slot is freed and the next prefetched file starts
parsing instead of the slot idling (``gate().active`` + ``task.phase`` tell the
coordinator who still holds / waits). ``POST /cancel-batch`` is the 【取消全部】
entry point: it stops the coordinator from dispatching any further file AND
cancels every queued / running task of the batch (remaining PENDING shells are
finalized immediately).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.config import PromptsConfig, get_config
from ..core.concurrency import gate, set_concurrency
from ..core.paths import get_layout
from ..core.tasks import TERMINAL, TaskStatus, get_task_manager
from ..engines import script as S
from ..engines.script_prompts import load_default_prompts
from . import _common

router = APIRouter(prefix="/api/script", tags=["script"])

# 预取深度：稳态下「持槽解析的任务数 + 等待槽的预取任务数」= 并发数 + 4。
# 需求固定值（并发 8 → 最多 12 个任务在调度队列内）。
PREFETCH_DEPTH = 4

# 协调者轮询间隔（秒）。
_COORDINATOR_POLL_S = 0.2


def _resolved_prompts() -> PromptsConfig:
    """The configured prompts, with empty fields filled from the bundled defaults.

    Mirrors the source ``get_config`` behaviour. Returns a copy, so the shared
    in-memory config is never mutated as a side effect of a generate call.
    """
    prompts = get_config().prompts
    system_prompt, user_prompt = load_default_prompts()
    return prompts.model_copy(update={
        "system_prompt": prompts.system_prompt or system_prompt,
        "user_prompt": prompts.user_prompt or user_prompt,
    })


def _resolve_split_file(name: str) -> Path:
    """Resolve a ``02_split_text`` file name to an absolute path, rejecting traversal.

    The frontend sends bare file names (from ``GET /api/files/list/02_split_text``);
    anything that resolves outside the ``02_split_text`` directory is refused.
    """
    base = get_layout().split_text
    if base is None:
        raise HTTPException(409, "尚未设置工作空间——请先在「开始」页选择文件夹。")
    candidate = base / name
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        raise HTTPException(400, f"非法文件路径：{name}")
    if not candidate.is_file():
        raise HTTPException(400, f"文件不存在：{name}")
    return candidate


# --------------------------------------------------------------------------- #
# 批次注册表（进程级内存态；重启即失——F5 恢复不依赖它，靠 label + task.seq 序）
# --------------------------------------------------------------------------- #

@dataclass
class ParseBatch:
    """One 【开始处理】 click: the ordered task_ids of its files + a stop flag."""

    id: str
    ordered: list[str]  # task_id，用户勾选顺序（= task.seq 升序）
    stop: threading.Event = field(default_factory=threading.Event)


_BATCHES: dict[str, ParseBatch] = {}
_TASK_BATCH: dict[str, str] = {}  # task_id -> batch_id
_LOCK = threading.Lock()


def _batch_of(task_id: str) -> ParseBatch | None:
    with _LOCK:
        bid = _TASK_BATCH.get(task_id)
        return _BATCHES.get(bid) if bid else None


def _cancel_pending_shells(batch: ParseBatch) -> None:
    """Finalize every still-PENDING shell of the batch (immediate, no polling wait)."""
    mgr = get_task_manager()
    for tid in batch.ordered:
        t = mgr.get(tid)
        if t is not None and t.status is TaskStatus.PENDING:
            mgr.control(tid, "cancel")


def _run_coordinator(batch: ParseBatch) -> None:
    """Dispatch a batch's PENDING shells in order, keeping the prefetch bounded.

    Invariant maintained each round: the number of batch tasks *waiting for a slot*
    (started, not yet slot-holding, and not yet released into the check stages) is
    < PREFETCH_DEPTH. Slot holders are read from the process-wide ``gate().active``
    (only parse workers hold slots) and released-into-check tasks are recognized via
    ``task.phase == "check"`` — so a file entering its mechanical check stage frees
    its slot for the next prefetched file immediately, instead of idling it.
    """
    mgr = get_task_manager()
    try:
        while True:
            with _LOCK:
                if batch.id not in _BATCHES:
                    break  # deregistered (batch cancel / endpoint cleanup)
            if batch.stop.is_set():
                _cancel_pending_shells(batch)
                break
            tasks = [mgr.get(tid) for tid in batch.ordered]
            if all(t is None or t.status in TERMINAL for t in tasks):
                break
            # Waiting = started-but-not-slot-holding, minus the ones already in the
            # (slot-free) check stages. gate().active is process-wide: other batches'
            # holders make this conservative (under-estimate), never over.
            no_slot = sum(
                1 for t in tasks
                if t is not None and t.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
                and t.phase != "check"
            )
            waiting = max(0, no_slot - gate().active)
            while waiting < PREFETCH_DEPTH:
                next_tid = next(
                    (tid for tid, t in zip(batch.ordered, tasks)
                     if t is not None and t.status is TaskStatus.PENDING),
                    None,
                )
                if next_tid is None:
                    break
                try:
                    mgr.start(next_tid)
                except (ValueError, KeyError):
                    break  # raced with a cancel — it will no longer be PENDING
                tasks = [mgr.get(tid) for tid in batch.ordered]
                no_slot = sum(
                    1 for t in tasks
                    if t is not None and t.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
                    and t.phase != "check"
                )
                waiting = max(0, no_slot - gate().active)
            time.sleep(_COORDINATOR_POLL_S)
    finally:
        with _LOCK:
            _BATCHES.pop(batch.id, None)
            for tid in batch.ordered:
                _TASK_BATCH.pop(tid, None)


class GenerateFilesRequest(BaseModel):
    files: list[str]  # 02_split_text 下的文件名（不含路径）


@router.post("/generate-files")
def generate_files(req: GenerateFilesRequest) -> dict:
    """Start one parse Task per selected ``02_split_text`` file.

    All task shells are created up front (PENDING, in request order) so the response
    carries every task_id; a coordinator thread then starts them in order with a
    bounded prefetch (see module docstring). The shared gate is sized from
    ``config.generation.max_concurrency``.
    """
    _common.require_workspace()
    names = list(dict.fromkeys(req.files))  # dedupe, preserving order
    if not names:
        raise HTTPException(400, "请选择要解析的文件。")
    cfg = get_config()
    set_concurrency(cfg.generation.max_concurrency)  # size the shared gate for this batch
    prompts = _resolved_prompts()
    mgr = get_task_manager()
    batch = ParseBatch(id=uuid.uuid4().hex[:12], ordered=[])
    created = []
    for name in names:
        path = _resolve_split_file(name)
        task = mgr.create(
            "script", f"文本解析（{name}）",
            S.generate_file,
            str(path), cfg.llm, prompts, cfg.generation,
            start=False,  # PENDING 壳——协调者按序 start，不一次性全部提交
        )
        batch.ordered.append(task.id)
        created.append({"file": name, "task_id": task.id})
    with _LOCK:
        _BATCHES[batch.id] = batch
        for tid in batch.ordered:
            _TASK_BATCH[tid] = batch.id
    threading.Thread(target=_run_coordinator, args=(batch,), daemon=True).start()
    return {"task_ids": [c["task_id"] for c in created], "files": created}


class CancelBatchRequest(BaseModel):
    task_ids: list[str]


@router.post("/cancel-batch")
def cancel_batch(req: CancelBatchRequest) -> dict:
    """【取消全部】：取消给定任务 + 停止其所属批次继续投放。

    For every batch the given tasks belong to: set the stop flag (the coordinator
    will not dispatch further files) and finalize the batch's remaining PENDING
    shells immediately; then cancel each requested non-terminal task (running /
    waiting ones honour it at the next cooperative check — queued ones within ~0.2 s
    via the gate's stop_check). Idempotent: terminal / unknown ids are ignored.
    """
    mgr = get_task_manager()
    cancelled: list[dict] = []
    batches_stopped = 0
    seen: set[str] = set()
    for tid in dict.fromkeys(req.task_ids):
        task = mgr.get(tid)
        if task is None or task.status in TERMINAL:
            continue
        batch = _batch_of(tid)
        if batch is not None and batch.id not in seen:
            batch.stop.set()
            _cancel_pending_shells(batch)
            seen.add(batch.id)
            batches_stopped += 1
        mgr.control(tid, "cancel")
        cancelled.append(task.snapshot())
    return {"cancelled": cancelled, "batches_stopped": batches_stopped}
