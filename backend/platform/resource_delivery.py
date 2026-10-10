"""Delivery eligibility from successful production tasks, never folder alone."""
from __future__ import annotations

from pathlib import Path
from datetime import timezone

from sqlalchemy import select

from ..core.safe_filesystem import file_identity, identity_matches, safe_regular_path
from .models import ProjectFile, Task, TaskResult
from .storage import safe_project_workspace_path

POLICY_VERSION = 1
AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
DELIVERY_TYPES = {
    "tts.merge": ({"06_audio_merge"}, "纯旁白成品"),
    "bgm.mix": ({"08_bgm"}, "混音成品"),
    "bgm.package": ({"08_bgm"}, "混音合集"),
}
ARCHIVE_TYPES = {"bgm.package"}


class DeliveryDenied(ValueError):
    pass


def _relative(root: Path, value: str) -> str | None:
    try:
        path = Path(value)
        relative = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
        safe_regular_path(root, relative)
        return relative
    except (OSError, ValueError, RuntimeError):
        return None


def _allowed(task_type: str, relative: str, result: dict) -> bool:
    modules, _label = DELIVERY_TYPES[task_type]
    path = Path(relative)
    if path.parts[0] not in modules:
        return False
    if task_type in ARCHIVE_TYPES:
        return path.suffix.lower() == ".zip" and result.get("delivery_policy") == POLICY_VERSION
    return path.suffix.lower() in AUDIO_SUFFIXES


def capture_deliveries(db, user, project_id: str, task_type: str, result: dict, extra_paths=()) -> list[dict]:
    if task_type not in DELIVERY_TYPES or result.get("complete") is False:
        return []
    root = safe_project_workspace_path(db, user.username, project_id)
    if root is None:
        return []
    values = [result.get("path", ""), *extra_paths]
    values.extend(item.get("path", "") for item in result.get("files", []) if isinstance(item, dict))
    deliveries = {}
    for value in values:
        relative = _relative(root, str(value)) if value else None
        if relative and _allowed(task_type, relative, result):
            stat = safe_regular_path(root, relative).stat()
            if stat.st_size:
                deliveries[relative] = {"relative_path": relative, "identity": list(file_identity(stat))}
    return list(deliveries.values())


def _historical_delivery_records(db, user, project_id: str) -> dict[str, dict]:
    root = safe_project_workspace_path(db, user.username, project_id)
    if root is None:
        return {}
    rows = db.execute(select(Task, TaskResult).join(TaskResult, TaskResult.task_id == Task.id).where(
        Task.owner_id == user.id, Task.project_id == project_id, Task.status == "succeeded",
        Task.task_type.in_(DELIVERY_TYPES),
    ).order_by(Task.finished_at.desc(), Task.id.desc())).all()
    records = {}
    seen = set()
    for task, stored in rows:
        result = stored.result
        if result.get("complete") is False:
            values = [result, *[item for item in result.get("files", []) if isinstance(item, dict)]]
            for item in values:
                relative = _relative(root, str(item.get("path"))) if item.get("path") else None
                if relative:
                    seen.add(relative)
            continue
        stamped = "deliveries" in result
        candidates = result.get("deliveries", []) if stamped else []
        if not stamped and task.task_type not in ARCHIVE_TYPES:
            # Historical task results have engine-produced paths. Validate size
            # and publication time; unknown/untracked files remain unavailable.
            values = [result, *[item for item in result.get("files", []) if isinstance(item, dict)]]
            for item in values:
                path = item.get("path")
                catalog = db.get(ProjectFile, item.get("file_id")) if item.get("file_id") else None
                if catalog and (catalog.owner_id != user.id or catalog.project_id != project_id or catalog.deleted_at):
                    catalog = None
                if not path and catalog and catalog.owner_id == user.id and catalog.project_id == project_id and not catalog.deleted_at:
                    # Storage usernames are sanitized independently.
                    parts = catalog.object_key.split("/", 2)
                    path = parts[2] if len(parts) == 3 and parts[1] == project_id else None
                relative = _relative(root, str(path)) if path else None
                if not relative or not _allowed(task.task_type, relative, result):
                    continue
                stat = safe_regular_path(root, relative).stat()
                expected = item.get("size") if item.get("path") else catalog.size_bytes if catalog else None
                if expected is None and catalog and Path(catalog.object_key).suffix.lower() == Path(relative).suffix.lower():
                    expected = catalog.size_bytes
                if expected is None and item.get("file") == Path(relative).name:
                    expected = stat.st_size
                finished = task.finished_at.replace(tzinfo=timezone.utc) if task.finished_at and task.finished_at.tzinfo is None else task.finished_at
                if expected is None or stat.st_size != expected or not finished or max(stat.st_mtime, stat.st_ctime) > finished.timestamp() + 1:
                    continue
                candidates.append({"relative_path": relative, "identity": list(file_identity(stat))})
        for item in candidates:
            relative = item.get("relative_path", "")
            if relative in seen or not relative or not _allowed(task.task_type, relative, result):
                continue
            seen.add(relative)
            try:
                stat = safe_regular_path(root, relative).stat()
                if stat.st_size and identity_matches(file_identity(stat), item.get("identity")):
                    records[relative] = {**item, "task_id": task.id, "label": DELIVERY_TYPES[task.task_type][1], "completed_at": task.finished_at.isoformat() if task.finished_at else None, "version": f"V{task.finished_at.strftime('%Y%m%d-%H%M%S')}" if task.finished_at else None}
            except (OSError, ValueError, RuntimeError):
                continue
    return records


def delivery_records(db, user, project_id: str, relatives=None) -> dict[str, dict]:
    from .models import DeliveryIndexState
    state = db.get(DeliveryIndexState, project_id)
    if state is not None and state.complete:
        from .delivery_index import indexed_records
        root = safe_project_workspace_path(db, user.username, project_id)
        return indexed_records(db, user, project_id, root, relatives) if root is not None else {}
    records = _historical_delivery_records(db, user, project_id)
    return records if relatives is None else {name: records[name] for name in relatives if name in records}


def require_delivery(db, user, project_id: str, relative: str) -> dict:
    record = delivery_records(db, user, project_id, [relative]).get(relative)
    if record is None:
        raise DeliveryDenied("此资源为制作资料或尚未形成有效成品，不提供下载或打包。")
    return record


def validate_delivery_sources(db, user, project_id: str, task_type: str, payload: dict) -> dict:
    if task_type != "bgm.package":
        return {}
    files = [f"08_bgm/{stem}.mp3" for stem in payload.get("chapters", [])]
    records = delivery_records(db, user, project_id, files)
    if not files or any(relative not in records for relative in files):
        raise DeliveryDenied("只允许导出已完成的音频成品，制作资料不能打包或导出。")
    return {relative: records[relative] for relative in files}


def capabilities(record: dict | None, *, preview: bool) -> dict:
    return {
        "can_preview": preview, "can_download": bool(record), "can_package": bool(record),
        "resource_role": "deliverable" if record else "production",
        "delivery_label": record["label"] if record else None,
        "completed_at": record.get("completed_at") if record else None,
        "delivery_version": record.get("version") if record else None,
        "download_reason": None if record else "制作资料仅供检查，请前往对应工作台。",
    }
