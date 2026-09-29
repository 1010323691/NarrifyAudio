"""Text-format workbench: server-side orchestration of the layout/split
pipeline, human review marks, and versioned reads/exports.

Layering: ``api/text_format.py`` -> this service -> platform. All state is
rebuildable from the database (flows, tasks, results, project files); the
browser only tracks task progress and renders. Recovery therefore never
resubmits an existing stage — missing stages are submitted here with the
stable idempotency keys ``tflow:{flow_id}:{stage}``.
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.models import ChapterReviewMark, Project, ProjectFile, Task, TextFormatFlow, User
from ..platform.storage import configured_storage_root, object_path
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..platform.task_submission import submit_task_record
from .task_operations import owned_project

FLOW_TASK_TYPES = ("text.format", "book.analyze", "book.split")
_SPLIT_MODULE = "02_split_text"
PREVIEW_MAX_BYTES = 8 * 1024 * 1024


class WorkbenchError(Exception):
    def __init__(self, status_code: int, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.detail = detail or {}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _task_lite(db: Session, task_id: str | None) -> dict | None:
    if not task_id:
        return None
    task = db.get(Task, task_id)
    if task is None:
        return None
    return {
        "id": task.id, "task_type": task.task_type, "status": task.status,
        "progress": task.progress, "error_code": task.error_code,
        "error_message": task.error_message, "created_at": _iso(task.created_at),
        "finished_at": _iso(task.finished_at),
    }


def _flow_dict(flow: TextFormatFlow) -> dict:
    return {
        "id": flow.id, "source_file_id": flow.source_file_id,
        "config_snapshot": flow.config_snapshot or {}, "whole_book": flow.whole_book,
        "format_task_id": flow.format_task_id, "analyze_task_id": flow.analyze_task_id,
        "split_task_id": flow.split_task_id, "split_mode": flow.split_mode,
        "status": flow.status, "error": flow.error,
        "manifest_count": len(flow.manifest or []),
        "created_at": _iso(flow.created_at), "updated_at": _iso(flow.updated_at),
    }


def _flow_task_ids(flow: TextFormatFlow) -> set[str]:
    return {t for t in (flow.format_task_id, flow.analyze_task_id, flow.split_task_id) if t}


def _latest_flow(db: Session, project_id: str, owner_id: str, source_file_id: str | None = None) -> TextFormatFlow | None:
    stmt = select(TextFormatFlow).where(TextFormatFlow.project_id == project_id, TextFormatFlow.owner_id == owner_id)
    if source_file_id:
        stmt = stmt.where(TextFormatFlow.source_file_id == source_file_id)
    return db.scalar(stmt.order_by(TextFormatFlow.updated_at.desc(), TextFormatFlow.id.desc()).limit(1))


def _active_flow_tasks(db: Session, owner_id: str, project_id: str) -> list[Task]:
    return list(db.scalars(select(Task).where(
        Task.owner_id == owner_id, Task.project_id == project_id,
        Task.task_type.in_(list(FLOW_TASK_TYPES)), Task.status.in_(sorted(ACTIVE_TASK_STATUSES)),
    )))


def _validate_source_file(db: Session, project: Project, user: User, source_file_id: str) -> ProjectFile:
    item = db.scalar(select(ProjectFile).where(
        ProjectFile.id == source_file_id,
        ProjectFile.project_id == project.id,
        ProjectFile.owner_id == user.id,
        ProjectFile.deleted_at.is_(None),
    ))
    if item is None:
        raise WorkbenchError(404, "源文件不存在或已删除")
    parts = item.object_key.split("/")
    if len(parts) < 3 or parts[2] != "01_input":
        raise WorkbenchError(422, "源文件必须来自原始文件（01_input）目录")
    return item


def _task_result(db: Session, task_id: str) -> dict:
    task = db.get(Task, task_id)
    if task is None or task.result is None:
        return {}
    return task.result.result or {}


def _capture_manifest(db: Session, user: User, project: Project, split_result: dict) -> list[dict] | None:
    """Record the published files (name -> sha256) of a successful split so
    later reads can prove they still match the live artifacts. Returns None
    when the published set is incomplete (another flow interfered)."""
    entries: list[dict] = []
    for f in split_result.get("files") or []:
        file_id = f.get("file_id")
        record = db.get(ProjectFile, file_id) if file_id else None
        if record is None or record.deleted_at is not None:
            return None
        if record.project_id != project.id or record.owner_id != user.id:
            return None
        entries.append({
            "name": f.get("name"), "object_key": record.object_key,
            "sha256": record.sha256, "size_bytes": record.size_bytes,
        })
    if not entries:
        return None
    return entries


def _manifest_consistent(db: Session, user: User, project: Project, flow: TextFormatFlow) -> bool:
    manifest = flow.manifest or []
    if not manifest:
        return False
    for entry in manifest:
        live = db.scalar(select(ProjectFile).where(
            ProjectFile.object_key == entry["object_key"],
            ProjectFile.project_id == project.id,
            ProjectFile.owner_id == user.id,
            ProjectFile.deleted_at.is_(None),
        ))
        if live is None or live.sha256 != entry["sha256"] or live.size_bytes != entry["size_bytes"]:
            return False
    return True


# ---------------------------------------------------------------------------
# Review matters (P0-04): the server normalizes actions + warnings + removed
# into user-facing matters; the frontend renders them without its own rule set.
# ---------------------------------------------------------------------------

_ADVISORY_REASON_TEXT = {
    "duplicate_number": "章节号重复——原编号重复，已按原文顺序重新编号，正文已保留。",
    "duplicate_split": "疑似正文重复——重复内容已拆分保留，请核对是否误判。",
    "duplicate_kept": "章节重复或疑似内容重复，请核对。",
    "inferred": "推断拆分——章节边界由引擎推断，建议对照原文核对。",
    "mechanical": "机械拆分——按结构线索拆分，建议对照原文核对。",
    "truncated": "已截除重复正文——仅保留首次出现，请核对删除范围。",
    "range_mid": "范围标题补齐——部分切点落在段落中间，请人工核对。",
}
_INFO_REASON_TEXT = {
    "renumbered": "已规范编号",
    "gap_absorbed": "原文跳号，已并入连续编号",
    "range": "范围标题已按段落边界补齐",
    "length_split": "按字数分册",
}
_ACTION_TO_REASON = {
    "duplicate_kept": "duplicate_kept",
    "duplicate_truncated": "truncated",
    "inferred_split": "inferred",
    "mechanical_split": "mechanical",
    "range_split": "range",
    "renumbered": "renumbered",
    "gap_absorbed": "gap_absorbed",
    "length_split": "length_split",
    "kept": "kept",
}


def _chapter_reasons(chapter: dict) -> list[str]:
    reasons = chapter.get("reasons")
    if isinstance(reasons, list) and reasons:
        return [str(r) for r in reasons]
    actions = chapter.get("actions") or []
    return [_ACTION_TO_REASON.get(str(a), str(a)) for a in actions] or ["kept"]


def build_review_matters(split_result: dict, mode: str | None) -> tuple[list[dict], list[dict]]:
    """Return (chapters with derived fields, version-level matters)."""
    report = split_result.get("report") or {}
    warnings = report.get("warnings") or []
    removed = report.get("removed") or []
    mid_paragraph = any(str(w.get("type")) == "range_header_split_mid_paragraph" for w in warnings)

    matters: list[dict] = []

    def add_matter(scope: str, reason: str, text: str, *, advisory: bool, chapter_key: str | None = None, detail: str | None = None) -> dict:
        matter = {
            "id": f"m{len(matters) + 1}", "scope": scope, "reason": reason,
            "advisory": advisory, "text": text,
        }
        if chapter_key:
            matter["chapter_key"] = chapter_key
        if detail:
            matter["detail"] = detail
        matters.append(matter)
        return matter

    chapters: list[dict] = []
    for ch in split_result.get("chapters") or []:
        final_num = ch.get("final_num")
        key = f"c{final_num}" if final_num is not None else None
        reasons = _chapter_reasons(ch)
        chapter_matter_ids: list[str] = []
        pending = False
        for reason in reasons:
            if reason == "kept":
                continue
            if reason == "range" and mid_paragraph:
                label = "range_mid"
            elif reason in _INFO_REASON_TEXT:
                label = reason
            elif reason in _ADVISORY_REASON_TEXT:
                label = reason
            else:
                # Unknown engine reason: surface it, ask for review (fail open).
                label = reason
            if label in _ADVISORY_REASON_TEXT:
                chapter_matter_ids.append(add_matter("chapter", label, _ADVISORY_REASON_TEXT[label], advisory=True, chapter_key=key)["id"])
                pending = True
            else:
                chapter_matter_ids.append(add_matter("chapter", label, _INFO_REASON_TEXT.get(label, label), advisory=False, chapter_key=key)["id"])
        chapters.append({
            **ch, "key": key, "reasons": reasons, "pending": pending,
            "adjusted": any(r != "kept" for r in reasons),
            "matters": chapter_matter_ids,
        })

    # Version-level: removed chapters keep a verification entry (P0-04).
    for entry in removed:
        add_matter("version", "removed", f"已删除重复章节：第 {entry.get('numStr') or '?'} 章「{entry.get('title') or '无标题'}」",
                   advisory=True, detail="该章节未进入最终结果，可在处理记录中查看依据。")

    # Version-level: warnings. Chapter-attributable ones (range mid-paragraph)
    # already appear as chapter matters; here keep the raw detail as a version
    # note so the 处理记录 tab holds the full audit trail.
    for w in warnings:
        wtype = str(w.get("type", ""))
        advisory = wtype in {"range_header_split_mid_paragraph", "range_header_split_skipped"}
        add_matter("version", f"warning:{wtype}", str(w.get("detail") or wtype), advisory=advisory)

    if mode == "by_length":
        count = len(split_result.get("files") or [])
        target = split_result.get("length_target")
        add_matter("version", "length_fallback",
                  f"未识别到章节，已按约 {target or 3000} 字/册将全文拆分为 {count} 册；切点落在段落/句子边界。",
                  advisory=False)
    elif mode == "whole_book":
        add_matter("version", "whole_book", "按整本继续：未做章节拆分，整本书作为单一文件输出。", advisory=False)

    return chapters, matters


def _version_from_flow(db: Session, user: User, project: Project, flow: TextFormatFlow) -> dict | None:
    if flow.status != "ready" or not flow.split_task_id:
        return None
    task = db.get(Task, flow.split_task_id)
    if task is None or task.result is None:
        return None
    result = task.result.result or {}
    mode = flow.split_mode
    chapters, matters = build_review_matters(result, mode)
    if mode == "whole_book" and not chapters:
        chapters = [{
            "key": "whole", "seq": 1, "num": None, "numStr": "", "title": "整本",
            "chars": sum(f.get("chars") or 0 for f in result.get("files") or []),
            "orig_num": None, "orig_numStr": "", "final_num": None,
            "actions": ["whole_book"], "reasons": ["whole_book"],
            "confidence": "high", "pending": False, "adjusted": False, "matters": [],
        }]
    consistent = _manifest_consistent(db, user, project, flow)
    return {
        "flow_id": flow.id, "task_id": flow.split_task_id, "mode": mode,
        "version_status": "current" if consistent else "stale",
        "created_at": _iso(task.created_at),
        "total_chars": sum(f.get("chars") or 0 for f in result.get("files") or []),
        "chapters": chapters,
        "files": [{"file_id": f.get("file_id"), "name": f.get("name"), "chars": f.get("chars")} for f in result.get("files") or []],
        "matters": matters,
        "report": {
            "actions": result.get("report", {}).get("actions", []),
            "warnings": result.get("report", {}).get("warnings", []),
            "removed": result.get("report", {}).get("removed", []),
        },
        "baseline_chars": result.get("baseline_chars"),
        "original_count": result.get("original_count"),
        "length_target": result.get("length_target"),
        "review_marks": sorted(m.chapter_key for m in db.scalars(
            select(ChapterReviewMark).where(ChapterReviewMark.task_id == flow.split_task_id)
        )),
    }


# ---------------------------------------------------------------------------
# Flow orchestration
# ---------------------------------------------------------------------------

def _advance(db: Session, user: User, project: Project, flow: TextFormatFlow) -> None:
    """Evaluate stage progress and idempotently submit missing stages."""
    flow.error = None
    if flow.status == "ready":
        return
    flow.status = "running"

    # Stage 1: text.format
    if flow.format_task_id is None:
        source = db.get(ProjectFile, flow.source_file_id)
        if source is None or source.deleted_at is not None:
            flow.status = "failed"
            flow.error = "源文件不可用，无法继续排版"
            return
        stem = Path(source.original_name).stem or "text"
        task = submit_task_record(db, user, project_id=project.id, task_type="text.format",
            payload={
                "input_file_id": source.id, "config": flow.config_snapshot or {},
                "publish_module": "01_input", "output_name": f"{stem}_排版.txt",
            }, idempotency_key=f"tflow:{flow.id}:format")
        flow.format_task_id = task.id
        return
    fmt_task = db.get(Task, flow.format_task_id)
    if fmt_task is None or fmt_task.status in {"failed", "cancelled", "timeout"}:
        flow.status = "failed"
        flow.error = (fmt_task.error_message if fmt_task else "") or "排版任务失败"
        return
    if fmt_task.status != "succeeded":
        return

    # Stage 2: book.analyze
    if flow.analyze_task_id is None:
        file_id = _task_result(db, flow.format_task_id).get("file_id")
        if not file_id:
            flow.status = "failed"
            flow.error = "排版产物缺少文件标识，无法继续分析"
            return
        task = submit_task_record(db, user, project_id=project.id, task_type="book.analyze",
            payload={"input_file_id": file_id}, idempotency_key=f"tflow:{flow.id}:analyze")
        flow.analyze_task_id = task.id
        return
    ana_task = db.get(Task, flow.analyze_task_id)
    if ana_task is None or ana_task.status in {"failed", "cancelled", "timeout"}:
        flow.status = "failed"
        flow.error = (ana_task.error_message if ana_task else "") or "章节分析任务失败"
        return
    if ana_task.status != "succeeded":
        return

    # Stage 3: book.split
    if flow.split_task_id is None:
        analysis = (_task_result(db, flow.analyze_task_id).get("analysis")) or {}
        chapter_count = analysis.get("chapter_count") or 0
        # All split branches read the formatted output (same input analyze used).
        fmt_file_id = _task_result(db, flow.format_task_id).get("file_id")
        if not fmt_file_id:
            flow.status = "failed"
            flow.error = "排版产物缺少文件标识，无法分册"
            return
        base = {"input_file_id": fmt_file_id}
        if flow.whole_book:
            mode, payload = "whole_book", {**base, "whole_book": True}
        elif chapter_count > 0:
            mode, payload = "smart", {**base, "smart": True}
        else:
            # Zero chapters: fall back to by-length split (V2 规格 4.2 兜底)。
            mode, payload = "by_length", {**base, "by_length": True}
        flow.split_mode = mode
        task = submit_task_record(db, user, project_id=project.id, task_type="book.split",
            payload=payload, idempotency_key=f"tflow:{flow.id}:split")
        flow.split_task_id = task.id
        return
    split_task = db.get(Task, flow.split_task_id)
    if split_task is None or split_task.status in {"failed", "cancelled", "timeout"}:
        flow.status = "failed"
        flow.error = (split_task.error_message if split_task else "") or "分册任务失败"
        return
    if split_task.status != "succeeded":
        return
    if flow.manifest is None:
        manifest = _capture_manifest(db, user, project, _task_result(db, flow.split_task_id))
        if manifest is None:
            flow.status = "failed"
            flow.error = "分册产物不完整（文件发布异常），请重试"
            return
        flow.manifest = manifest
    flow.status = "ready"


def start_or_continue_flow(
    db: Session, user: User, project_id: str, *,
    source_file_id: str | None = None, config: dict | None = None,
    whole_book: bool = False, restart: bool = False,
) -> dict:
    project = owned_project(db, user.id, project_id)
    if project is None:
        raise WorkbenchError(404, "项目不存在")

    active = _active_flow_tasks(db, user.id, project_id)
    flow = _latest_flow(db, project.id, user.id, source_file_id)
    if restart:
        if active:
            raise WorkbenchError(409, "有排版分册任务正在进行，暂时不能重新处理")
        if flow is not None and flow.source_file_id == source_file_id and flow.status != "running":
            # 重新处理：旧版本（成功或失败）保留为历史，新建 flow 重跑。
            # running 的 flow 必有活跃任务，已被上面的 409 拦住。
            flow = None
    if flow is None or flow.status == "ready":
        if not restart and flow is not None:
            # A ready flow for this file needs no further action.
            db.commit()
            return flow_state(db, user, project_id)
        if not source_file_id:
            raise WorkbenchError(422, "请先选择要处理的 TXT 文件")
        _validate_source_file(db, project, user, source_file_id)
        if active:
            # Brand-new flow: any in-flight pipeline task (from another
            # session/file) blocks the whole project (P0-02 server-side guard).
            raise WorkbenchError(409, "有排版分册任务正在进行，暂时无法开始处理")
        flow = TextFormatFlow(
            project_id=project.id, owner_id=user.id, source_file_id=source_file_id,
            config_snapshot=config or {}, whole_book=bool(whole_book), status="running",
        )
        db.add(flow)
        db.flush()
    else:
        # Resuming an existing flow: only its own recorded tasks may be in
        # flight; anything else means another session is mid-flight.
        mine = _flow_task_ids(flow)
        for task in active:
            if task.id not in mine:
                raise WorkbenchError(409, "有排版分册任务正在进行，暂时无法继续")
    _advance(db, user, project, flow)
    db.commit()
    return flow_state(db, user, project_id)


def flow_state(db: Session, user: User, project_id: str) -> dict:
    project = owned_project(db, user.id, project_id)
    if project is None:
        raise WorkbenchError(404, "项目不存在")
    flow = _latest_flow(db, project.id, user.id)

    # Upgrade fallback (adopt): no flow rows yet, but a succeeded split task
    # exists — recover it so old data stays restorable.
    if flow is None:
        split_task = db.scalar(select(Task).where(
            Task.owner_id == user.id, Task.project_id == project.id,
            Task.task_type == "book.split", Task.status == "succeeded",
        ).order_by(Task.created_at.desc(), Task.id.desc()).limit(1))
        if split_task is not None:
            flow = TextFormatFlow(
                project_id=project.id, owner_id=user.id,
                source_file_id=str((_task_result(db, split_task.id).get("source_file_id") or "")) or "0",
                config_snapshot={}, whole_book=False, split_task_id=split_task.id,
                split_mode="smart" if _task_result(db, split_task.id).get("chapters") else "whole_book",
                status="ready",
            )
            db.add(flow)
            db.flush()
            flow.manifest = _capture_manifest(db, user, project, _task_result(db, split_task.id)) or []
            db.commit()
            flow = _latest_flow(db, project.id, user.id)

    active = _active_flow_tasks(db, user.id, project.id)
    version = None
    ready_flow = _latest_flow(db, project.id, user.id)
    if ready_flow is not None and ready_flow.status == "ready":
        version = _version_from_flow(db, user, project, ready_flow)

    next_task: dict | None = None
    if flow is not None and flow.status != "ready":
        for stage, task_id in (("format", flow.format_task_id), ("analyze", flow.analyze_task_id), ("split", flow.split_task_id)):
            task = db.get(Task, task_id) if task_id else None
            if task is not None and task.status in ACTIVE_TASK_STATUSES:
                next_task = {"stage": stage, "task_id": task.id, "task_type": task.task_type,
                             "status": task.status, "progress": task.progress}
                break
        if next_task is None:
            # Terminal without success recorded yet — surface the failure.
            for task_id in (flow.format_task_id, flow.analyze_task_id, flow.split_task_id):
                task = db.get(Task, task_id) if task_id else None
                if task is not None and task.status in {"failed", "cancelled", "timeout"}:
                    next_task = {"stage": None, "task_id": task.id, "task_type": task.task_type,
                                 "status": task.status, "progress": task.progress, "failed": True}
                    break

    return {
        "flow": _flow_dict(flow) if flow is not None else None,
        "version": version,
        "next_task": next_task,
        "active_tasks": [
            {"id": t.id, "task_type": t.task_type, "status": t.status, "progress": t.progress}
            for t in active
        ],
    }


# ---------------------------------------------------------------------------
# Review marks
# ---------------------------------------------------------------------------

def _mark_task(db: Session, user: User, project: Project, task_id: str) -> tuple[Task, dict]:
    task = db.get(Task, task_id)
    if task is None or task.project_id != project.id or task.owner_id != user.id:
        raise WorkbenchError(404, "指定的处理版本不存在")
    if task.task_type != "book.split" or task.status != "succeeded":
        raise WorkbenchError(404, "指定的处理版本不可用（任务未成功完成）")
    return task, (task.result.result if task.result else {}) or {}


def _valid_chapter_keys(result: dict) -> set[str]:
    keys = {f"c{c.get('final_num')}" for c in result.get("chapters") or [] if c.get("final_num") is not None}
    if not result.get("chapters") and result.get("file_count"):
        keys.add("whole")
    return keys


def mark_review(db: Session, user: User, project_id: str, task_id: str, chapter_key: str) -> dict:
    project = owned_project(db, user.id, project_id)
    if project is None:
        raise WorkbenchError(404, "项目不存在")
    _mark_task(db, user, project, task_id)
    _, result = _mark_task(db, user, project, task_id)
    if chapter_key not in _valid_chapter_keys(result):
        raise WorkbenchError(404, "该章节不属于此处理版本")
    row = db.scalar(select(ChapterReviewMark).where(
        ChapterReviewMark.task_id == task_id,
        ChapterReviewMark.chapter_key == chapter_key,
        ChapterReviewMark.project_id == project.id,
        ChapterReviewMark.owner_id == user.id,
    ))
    if row is None:
        row = ChapterReviewMark(project_id=project.id, owner_id=user.id, task_id=task_id, chapter_key=chapter_key)
        db.add(row)
    db.commit()
    return {"chapter_key": chapter_key, "marked_at": _iso(row.created_at)}


def unmark_review(db: Session, user: User, project_id: str, task_id: str, chapter_key: str) -> dict:
    project = owned_project(db, user.id, project_id)
    if project is None:
        raise WorkbenchError(404, "项目不存在")
    row = db.scalar(select(ChapterReviewMark).where(
        ChapterReviewMark.task_id == task_id,
        ChapterReviewMark.chapter_key == chapter_key,
        ChapterReviewMark.project_id == project.id,
        ChapterReviewMark.owner_id == user.id,
    ))
    if row is not None:
        db.delete(row)
        db.commit()
    return {"chapter_key": chapter_key, "ok": True}


# ---------------------------------------------------------------------------
# Versioned reads / exports (P0-06)
# ---------------------------------------------------------------------------

def _ready_flow(db: Session, user: User, project: Project, flow_id: str) -> TextFormatFlow:
    flow = db.get(TextFormatFlow, flow_id)
    if flow is None or flow.project_id != project.id or flow.owner_id != user.id:
        raise WorkbenchError(404, "指定的处理版本不存在")
    if flow.status != "ready" or not flow.manifest:
        raise WorkbenchError(409, "该处理版本尚未完成")
    return flow


def _live_record(db: Session, user: User, project: Project, entry: dict) -> ProjectFile:
    live = db.scalar(select(ProjectFile).where(
        ProjectFile.object_key == entry["object_key"],
        ProjectFile.project_id == project.id,
        ProjectFile.owner_id == user.id,
        ProjectFile.deleted_at.is_(None),
    ))
    if live is None or live.sha256 != entry["sha256"] or live.size_bytes != entry["size_bytes"]:
        raise WorkbenchError(409, "版本内容已变化（文件被后续处理覆盖），请刷新后重试")
    return live


def preview_path(db: Session, user: User, project: Project, flow_id: str, name: str) -> Path:
    flow = _ready_flow(db, user, project, flow_id)
    entry = next((m for m in flow.manifest or [] if m.get("name") == name), None)
    if entry is None:
        raise WorkbenchError(404, "该文件不属于此处理版本")
    live = _live_record(db, user, project, entry)
    if live.size_bytes > PREVIEW_MAX_BYTES:
        raise WorkbenchError(413, "文件较大，请直接下载查看")
    return object_path(live.object_key, configured_storage_root(db))


def build_split_zip(db: Session, user: User, project: Project, flow_id: str) -> tuple[bytes, str]:
    """Validate the whole manifest against live files, then build the complete
    zip in memory (MB scale). Missing/mismatched files fail the request
    before anything streams."""
    flow = _ready_flow(db, user, project, flow_id)
    root = configured_storage_root(db)
    records: list[tuple[str, Path]] = []
    for entry in flow.manifest or []:
        live = db.scalar(select(ProjectFile).where(
            ProjectFile.object_key == entry["object_key"],
            ProjectFile.project_id == project.id,
            ProjectFile.owner_id == user.id,
            ProjectFile.deleted_at.is_(None),
        ))
        if live is None or live.sha256 != entry["sha256"] or live.size_bytes != entry["size_bytes"]:
            missing = [e.get("name") for e in flow.manifest or []]
            raise WorkbenchError(409, "部分分册文件缺失或已被新版本覆盖，无法导出", {"missing": missing})
        records.append((entry.get("name") or entry["object_key"].rsplit("/", 1)[-1], object_path(live.object_key, root)))
    if not records:
        raise WorkbenchError(409, "该处理版本没有可导出的文件")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, path in records:
            archive.write(path, arcname=name)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()
