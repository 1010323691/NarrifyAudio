"""Rebuildable, read-only SQLite indexes for the user's complete resource inventory.

The index is not application state: Worker publishes immutable snapshots outside
project workspaces. API queries never traverse a workspace or re-catalog inputs.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import mimetypes
import os
import re
import sqlite3
import time
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.safe_filesystem import is_link_or_junction, safe_regular_path
from .models import Project, Task, TaskResult, User
from .storage import configured_storage_root, safe_project_workspace_path
from .task_lifecycle import ACTIVE_TASK_STATUSES, TERMINAL_TASK_STATUSES
from .resource_delivery import capabilities, delivery_records, POLICY_VERSION

RESOURCE_CATEGORIES = {
    "01_input": "原始文件", "02_split_text": "章节文本", "03_parsed_json": "解析结果",
    "04_voice_profiles": "角色资料", "05_audio_chunk": "合成片段", "06_audio_merge": "合并音频",
    "07_output": "最终成品", "08_bgm": "BGM资料与混音", "config": "项目配置",
    "logs": "项目日志", "00_temp": "临时缓存", ".cache": "临时缓存", "cache": "临时缓存",
    "other": "其他文件",
}
CACHE_MODULES = frozenset({"00_temp", ".cache", "cache"})
SYSTEM_MODULES = frozenset({"config", "logs"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
INTERNAL_DIRS = frozenset({".tasks", ".resources"})
EXPORT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_PREVIEW_BYTES = 1024 * 1024
_HEX_ID = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{32}$")


class ResourceError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def natural_key(value: str) -> str:
    return re.sub(r"\d+", lambda match: f"{int(match.group()):030d}", value.casefold())


def resource_id(project_id: str, relative: str) -> str:
    return f"{project_id}:{hashlib.sha256(relative.encode('utf-8')).hexdigest()}"


def split_resource_id(value: str) -> tuple[str, str]:
    project_id, separator, digest = value.partition(":")
    if not separator or not _HEX_ID.fullmatch(digest) or not project_id or Path(project_id).name != project_id:
        raise ResourceError("资源标识无效", 404)
    return project_id, digest


def internal_path(db: Session, owner_id: str, *parts: str) -> Path:
    root = configured_storage_root(db)
    current = root
    for part in (".resources", owner_id, *parts):
        if not part or part in {".", ".."} or "/" in part or "\\" in part or ":" in part:
            raise ResourceError("资源存储标识无效")
        current = current / part
        if is_link_or_junction(current):
            raise ResourceError("资源存储目录不可安全访问")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ResourceError("资源存储路径越界")
    return current


def owned_project(db: Session, user: User, project_id: str, *, lock: bool = False) -> Project:
    statement = select(Project).where(Project.id == project_id, Project.owner_id == user.id, Project.deleted_at.is_(None))
    project = db.scalar(statement.with_for_update() if lock else statement)
    if project is None:
        raise ResourceError("项目不存在", 404)
    return project


def project_root(db: Session, user: User, project_id: str) -> Path:
    owned_project(db, user, project_id)
    root = safe_project_workspace_path(db, user.username, project_id)
    if root is None or is_link_or_junction(root) or not root.is_dir():
        raise ResourceError("项目存储目录不存在或不可安全访问")
    return root


@contextmanager
def index_connection(path: Path) -> Iterator[sqlite3.Connection]:
    if is_link_or_junction(path) or not path.is_file():
        raise ResourceError("资源清单尚未就绪，请刷新资源")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def current_snapshot(db: Session, owner_id: str, project_id: str) -> tuple[Path, dict] | None:
    pointer = internal_path(db, owner_id, project_id, "current.json")
    try:
        if not pointer.is_file() or pointer.stat().st_size > 16 * 1024:
            return None
        value = json.loads(pointer.read_text(encoding="utf-8"))
        snapshot_id = value["snapshot_id"]
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
            return None
        path = internal_path(db, owner_id, project_id, f"{snapshot_id}.sqlite")
        with index_connection(path) as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()
        return path, json.loads(row["value"])
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return None


def active_scan(db: Session, owner_id: str, project_id: str) -> Task | None:
    tasks = db.scalars(select(Task).where(
        Task.owner_id == owner_id,
        Task.task_type == "resources.scan", Task.status.in_(ACTIVE_TASK_STATUSES),
    ).order_by(Task.created_at.desc())).all()
    return next((task for task in tasks if project_id in (task.payload.get("project_ids") or [task.project_id])), None)


def source_version(db: Session, owner_id: str, project_id: str) -> str:
    task = db.scalar(select(Task).where(
        Task.owner_id == owner_id, Task.project_id == project_id,
        ~Task.task_type.like("resources.%"),
        Task.status.in_(TERMINAL_TASK_STATUSES),
    ).order_by(Task.updated_at.desc(), Task.id.desc()).limit(1))
    project = db.get(Project, project_id)
    cleanups = db.scalars(select(Task).where(Task.owner_id == owner_id, Task.task_type == "resources.cleanup", Task.status.in_(TERMINAL_TASK_STATUSES)).order_by(Task.updated_at.desc())).all()
    cleanup = next((item for item in cleanups if any(reference.get("project_id") == project_id for reference in item.payload.get("snapshots", []))), None)
    return f"{project.updated_at.isoformat() if project else ''}:{task.id if task else ''}:{task.updated_at.isoformat() if task else ''}:{cleanup.id if cleanup else ''}:{cleanup.updated_at.isoformat() if cleanup else ''}"


def overview(db: Session, user: User, *, page: int | None = None, page_size: int = 12, query: str = "", sort: str = "recent", project_id: str | None = None) -> dict:
    projects = db.scalars(select(Project).where(Project.owner_id == user.id).order_by(Project.updated_at.desc())).all()
    all_projects = projects
    snapshots = {p.id: current_snapshot(db, user.id, p.id) for p in all_projects} if page is not None else {}
    total = 0
    if page is not None:
        visible = [p for p in all_projects if p.deleted_at is None and query.strip().casefold() in p.name.casefold()]
        if sort == "name": visible.sort(key=lambda p: (p.name.casefold(), p.id))
        elif sort == "size": visible.sort(key=lambda p: (-(snapshots[p.id][1].get("size_bytes", 0) if snapshots[p.id] else -1), p.id))
        else: visible.sort(key=lambda p: ((snapshots[p.id][1].get("latest_modified_at") or "") if snapshots[p.id] else "", p.id), reverse=True)
        total = len(visible)
        projects = [p for p in all_projects if p.id == project_id and p.deleted_at is None] if project_id else visible[(page-1)*page_size:page*page_size]
    rows = []
    trash_bytes = 0
    trash_unknown = 0
    scans = db.scalars(select(Task).where(Task.owner_id == user.id, Task.task_type == "resources.scan").order_by(Task.created_at.desc()).limit(100)).all()
    for project in projects:
        snapshot = snapshots.get(project.id) if page is not None else current_snapshot(db, user.id, project.id)
        summary = snapshot[1] if snapshot else None
        if project.deleted_at is not None:
            if summary:
                trash_bytes += summary["size_bytes"]
                trash_unknown += int(not summary["complete"])
            else:
                trash_unknown += 1
            continue
        scan = active_scan(db, user.id, project.id)
        last_scan = next((task for task in scans if project.id in (task.payload.get("project_ids") or [task.project_id])), None)
        scan_error = last_scan.error_message if last_scan is not None and last_scan.status in {"failed", "timeout"} else None
        if last_scan is not None and last_scan.result is not None:
            scan_error = next((item["message"] for item in last_scan.result.result.get("scan_errors", []) if item["project_id"] == project.id), scan_error)
        version = source_version(db, user.id, project.id)
        deliveries = delivery_records(db, user, project.id)
        delivery_count = delivery_bytes = production_audio_count = 0
        production_categories = []
        if snapshot:
            with index_connection(snapshot[0]) as connection:
                attach_deliveries(connection, deliveries)
                delivery_count, delivery_bytes = connection.execute("SELECT count(*),coalesce(sum(size_bytes),0) FROM entries WHERE relative_path IN (SELECT relative_path FROM eligible_deliveries)").fetchone()
                production_audio_count = connection.execute("SELECT count(*) FROM entries WHERE preview_kind='audio' AND module IN ('05_audio_chunk','06_audio_merge','07_output','08_bgm') AND relative_path NOT IN (SELECT relative_path FROM eligible_deliveries)").fetchone()[0]
                production_categories = [{"key": row[0], "label": RESOURCE_CATEGORIES[row[0]], "count": row[1], "size_bytes": row[2]} for row in connection.execute("SELECT module,count(*),sum(size_bytes) FROM entries WHERE kind='file' AND module NOT IN ('config','logs','00_temp','.cache','cache') AND relative_path NOT IN (SELECT relative_path FROM eligible_deliveries) GROUP BY module")]
        rows.append({
            "project_id": project.id, "name": project.name,
            "snapshot": summary, "scan_task_id": scan.id if scan else None,
            "scan_status": scan.status if scan else None,
            "scan_error": scan_error,
            "stale": summary is None or summary.get("source_version") != version,
            "delivery_count": delivery_count, "delivery_bytes": delivery_bytes,
            "production_audio_count": production_audio_count, "production_categories": production_categories,
        })
    exports = list_exports(db, user)
    categories: dict[str, dict] = {}
    aggregate_rows = [{"snapshot": snapshots[p.id][1] if snapshots[p.id] else None} for p in all_projects if p.deleted_at is None] if page is not None else rows
    if page is not None:
        trash_rows = [snapshots[p.id][1] if snapshots[p.id] else None for p in all_projects if p.deleted_at is not None]
        trash_bytes = sum(r["size_bytes"] for r in trash_rows if r)
        trash_unknown = sum(not r or not r["complete"] for r in trash_rows)
    for row in aggregate_rows:
        if not row["snapshot"]:
            continue
        for item in row["snapshot"]["categories"]:
            bucket = categories.setdefault(item["key"], {"key": item["key"], "label": item["label"], "count": 0, "size_bytes": 0})
            bucket["count"] += item["count"]
            bucket["size_bytes"] += item["size_bytes"]
    return {
        "projects": rows, "categories": sorted(categories.values(), key=lambda item: -item["size_bytes"]),
        "storage": {
            "project_bytes": sum(row["snapshot"]["size_bytes"] for row in aggregate_rows if row["snapshot"]),
            "project_complete": all(row["snapshot"] and row["snapshot"]["complete"] for row in aggregate_rows),
            "trash_bytes": trash_bytes, "trash_complete": trash_unknown == 0,
            "export_bytes": sum(item["stored_bytes"] for item in exports),
        }, "exports": exports,
        **({"pagination": {"total": total, "page": page, "page_size": page_size, "counts": {"all": sum(p.deleted_at is None for p in all_projects), "complete": sum(bool(r["snapshot"] and r["snapshot"]["complete"]) for r in aggregate_rows)}}} if page is not None else {}),
    }


def preview_kind(name: str, module: str) -> str | None:
    extension = Path(name).suffix.lower()
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension == ".json":
        return "config" if module == "config" else "json"
    if module == "config":
        return None
    if extension in {".txt", ".log", ".md", ".csv", ".srt", ".vtt"}:
        return "text"
    return None


def build_index(
    workspace: Path, destination: Path, *, project_id: str, version: str,
    check: Callable[[], None], progress: Callable[[int], None],
) -> dict:
    """Scan metadata with bounded batches and cancellation between entries."""
    if not workspace.is_dir() or is_link_or_junction(workspace):
        raise ResourceError("项目存储目录不存在或不可安全访问")
    snapshot_id = uuid4().hex
    scanned_at = datetime.now(timezone.utc).isoformat()
    errors: list[dict] = []
    categories: dict[str, dict] = {}
    latest_ns = 0
    files = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    connection = sqlite3.connect(destination)
    try:
        connection.executescript("""
            PRAGMA journal_mode=OFF;
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE entries(
                id TEXT PRIMARY KEY, relative_path TEXT UNIQUE NOT NULL, parent TEXT NOT NULL,
                name TEXT NOT NULL, module TEXT NOT NULL, kind TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL,
                device TEXT NOT NULL, inode TEXT NOT NULL, extension TEXT NOT NULL,
                preview_kind TEXT, sort_key TEXT NOT NULL
            );
            CREATE INDEX entries_parent_sort ON entries(parent,kind,sort_key);
            CREATE INDEX entries_module_sort ON entries(module,kind,sort_key);
            CREATE INDEX entries_size ON entries(kind,size_bytes);
            CREATE INDEX entries_time ON entries(kind,mtime_ns);
        """)
        pending = [workspace]
        batch = []
        while pending:
            directory = pending.pop()
            check()
            try:
                entries = os.scandir(directory)
            except OSError:
                if len(errors) < 50:
                    errors.append({"path": directory.relative_to(workspace).as_posix(), "message": "目录无法读取"})
                continue
            with entries:
                for entry in entries:
                    check()
                    path = Path(entry.path)
                    try:
                        if is_link_or_junction(path):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in INTERNAL_DIRS:
                                continue
                            safe_regular_path(workspace, path.relative_to(workspace).as_posix(), directory=True)
                            kind = "directory"
                            pending.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            kind = "file"
                        else:
                            continue
                        # Windows DirEntry.stat may report zero inode/device;
                        # Path.stat matches the identity used by open-file checks.
                        stat = path.stat(follow_symlinks=False)
                        relative = path.relative_to(workspace).as_posix()
                    except (OSError, ValueError, RuntimeError):
                        if len(errors) < 50:
                            errors.append({"path": path.relative_to(workspace).as_posix(), "message": "文件无法读取"})
                        continue
                    module = relative.split("/", 1)[0]
                    module = module if module in RESOURCE_CATEGORIES else "other"
                    if relative == "setting.json":
                        module = "config"
                    if kind == "file":
                        files += 1
                        latest_ns = max(latest_ns, stat.st_mtime_ns)
                        bucket = categories.setdefault(module, {"key": module, "label": RESOURCE_CATEGORIES[module], "count": 0, "size_bytes": 0})
                        bucket["count"] += 1
                        bucket["size_bytes"] += stat.st_size
                    batch.append((resource_id(project_id, relative), relative, "" if path.parent == workspace else path.relative_to(workspace).parent.as_posix(), entry.name, module, kind, stat.st_size if kind == "file" else 0, stat.st_mtime_ns, stat.st_ctime_ns, str(stat.st_dev), str(stat.st_ino), path.suffix.lower(), preview_kind(entry.name, module) if kind == "file" else None, natural_key(relative)))
                    if len(batch) >= 500:
                        connection.executemany("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                        batch.clear()
                        progress(files)
        if batch:
            connection.executemany("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        audio_count = connection.execute("SELECT count(*) FROM entries WHERE kind='file' AND preview_kind='audio' AND module IN ('05_audio_chunk','06_audio_merge','07_output','08_bgm')").fetchone()[0]
        summary = {
            "snapshot_id": snapshot_id, "scanned_at": scanned_at, "source_version": version,
            "complete": not errors, "errors": errors, "file_count": files,
            "size_bytes": sum(item["size_bytes"] for item in categories.values()),
            "categories": list(categories.values()), "audio_count": audio_count,
            "latest_modified_at": datetime.fromtimestamp(latest_ns / 1e9, timezone.utc).isoformat() if latest_ns else None,
        }
        connection.execute("INSERT INTO metadata VALUES('summary',?)", (json.dumps(summary, ensure_ascii=False),))
        connection.commit()
        return summary
    except BaseException:
        connection.close()
        destination.unlink(missing_ok=True)
        raise
    finally:
        connection.close()


def _filters(category: str, path: str, query: str, extension: str, *, directory_mode: bool) -> tuple[str, list]:
    clauses = []
    values: list = []
    if directory_mode and not query:
        clauses.append("parent=?")
        values.append(path)
    else:
        clauses.append("kind='file'")
        if path:
            clauses.append("relative_path LIKE ? ESCAPE '\\'")
            values.append(_like_literal(path.rstrip("/") + "/") + "%")
    if category in {"deliverables", "production"}:
        pass
    elif category == "all":
        clauses.append("module NOT IN ('config','logs','00_temp','.cache','cache')")
    elif category == "audio":
        clauses.append("preview_kind='audio' AND module IN ('05_audio_chunk','06_audio_merge','07_output','08_bgm')")
    elif category == "text":
        clauses.append("module IN ('01_input','02_split_text','03_parsed_json')")
    elif category == "system":
        clauses.append("module IN ('config','logs')")
    elif category == "cache":
        clauses.append("module IN ('00_temp','.cache','cache')")
    elif category in RESOURCE_CATEGORIES:
        clauses.append("module=?")
        values.append(category)
    else:
        raise ResourceError("资源分类无效", 422)
    if query:
        clauses.append("relative_path LIKE ? ESCAPE '\\'")
        values.append("%" + _like_literal(query) + "%")
    if extension:
        clauses.append("extension=?")
        values.append("." + extension.lower().lstrip("."))
    return " AND ".join(clauses), values


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def attach_deliveries(connection, records: dict) -> None:
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS eligible_deliveries(relative_path TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM eligible_deliveries")
    for relative, record in records.items():
        identity = record["identity"]
        connection.execute("INSERT OR IGNORE INTO eligible_deliveries SELECT relative_path FROM entries WHERE relative_path=? AND size_bytes=? AND mtime_ns=? AND ctime_ns=? AND device=? AND inode=?", [relative, *identity[:3], str(identity[3]), str(identity[4])])


def entry_json(row: sqlite3.Row, project: Project, snapshot: dict, delivery: dict | None = None) -> dict:
    return {
        "id": row["id"], "project_id": project.id, "project_name": project.name,
        "name": row["name"], "relative_path": row["relative_path"], "module": row["module"],
        "module_label": RESOURCE_CATEGORIES[row["module"]], "kind": row["kind"],
        "size_bytes": row["size_bytes"],
        "modified_at": datetime.fromtimestamp(row["mtime_ns"] / 1e9, timezone.utc).isoformat(),
        "extension": row["extension"], "preview_kind": row["preview_kind"],
        "snapshot_id": snapshot["snapshot_id"],
        **capabilities(delivery, preview=row["preview_kind"] is not None),
    }


def list_entries(
    db: Session, user: User, *, project_id: str | None = None, category: str = "all",
    path: str = "", query: str = "", extension: str = "", sort: str = "name",
    page: int = 1, page_size: int = 50, directory_mode: bool = False, role: str = "",
) -> dict:
    if role not in {"", "production", "deliverable"}:
        raise ResourceError("资源用途无效", 422)
    if path and ("\\" in path or ":" in path or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ResourceError("目录路径无效", 422)
    order = {"name": "sort_key ASC", "modified": "mtime_ns DESC,sort_key ASC", "size": "size_bytes DESC,sort_key ASC"}.get(sort)
    if order is None:
        raise ResourceError("排序方式无效", 422)
    projects = [owned_project(db, user, project_id)] if project_id else db.scalars(select(Project).where(Project.owner_id == user.id, Project.deleted_at.is_(None))).all()
    if path and not project_id:
        raise ResourceError("目录浏览必须指定项目", 422)
    where, values = _filters(category, path, query, extension, directory_mode=directory_mode)
    candidates = []
    snapshots = []
    total = 0
    size_bytes = 0
    incomplete = []
    offset = (page - 1) * page_size
    heap = []
    def sort_key(row, project):
        primary = -row["size_bytes"] if sort == "size" else -row["mtime_ns"] if sort == "modified" else row["sort_key"]
        return (row["kind"] != "directory", primary, row["sort_key"], project.id, row["relative_path"])
    # Merge ordered SQLite cursors rather than materializing every preceding
    # page from every project. Even deep cross-project pages keep only one row
    # per project and the requested page in Python memory.
    with ExitStack() as stack:
        for project in projects:
            snapshot = current_snapshot(db, user.id, project.id)
            if snapshot is None:
                incomplete.append(project.id)
                continue
            index, summary = snapshot
            snapshots.append({"project_id": project.id, "snapshot_id": summary["snapshot_id"]})
            if not summary["complete"]:
                incomplete.append(project.id)
            connection = stack.enter_context(index_connection(index))
            deliveries = delivery_records(db, user, project.id)
            attach_deliveries(connection, deliveries)
            project_where = where
            if category == "deliverables" or role == "deliverable":
                project_where += " AND relative_path IN (SELECT relative_path FROM eligible_deliveries)"
            elif category == "production" or role == "production":
                project_where += " AND module NOT IN ('config','logs','00_temp','.cache','cache') AND relative_path NOT IN (SELECT relative_path FROM eligible_deliveries)"
            count = connection.execute(f"SELECT count(*),coalesce(sum(size_bytes),0) FROM entries WHERE {project_where}", values).fetchone()
            total += count[0]
            size_bytes += count[1]
            if len(projects) == 1:
                rows = connection.execute(f"SELECT * FROM entries WHERE {project_where} ORDER BY kind ASC,{order},relative_path ASC LIMIT ? OFFSET ?", [*values, page_size, offset]).fetchall()
                candidates.extend(entry_json(row, project, summary, deliveries.get(row["relative_path"])) for row in rows)
            else:
                cursor = connection.execute(f"SELECT * FROM entries WHERE {project_where} ORDER BY kind ASC,{order},relative_path ASC", values)
                row = cursor.fetchone()
                if row is not None:
                    heapq.heappush(heap, (sort_key(row, project), project, summary, row, cursor, deliveries))
        consumed = 0
        while heap and consumed < offset + page_size:
            _key, project, summary, row, cursor, deliveries = heapq.heappop(heap)
            if consumed >= offset:
                candidates.append(entry_json(row, project, summary, deliveries.get(row["relative_path"])))
            consumed += 1
            next_row = cursor.fetchone()
            if next_row is not None:
                heapq.heappush(heap, (sort_key(next_row, project), project, summary, next_row, cursor, deliveries))
    return {
        "items": candidates, "total": total,
        "size_bytes": size_bytes, "page": page, "page_size": page_size,
        "snapshots": snapshots, "complete": not incomplete, "incomplete_projects": incomplete,
    }


def resolve_resource(db: Session, user: User, value: str, snapshot_id: str | None = None) -> tuple[Path, dict, sqlite3.Row]:
    project_id, _digest = split_resource_id(value)
    project = owned_project(db, user, project_id)
    snapshot = current_snapshot(db, user.id, project_id)
    if snapshot is None:
        raise ResourceError("资源清单尚未就绪")
    index, summary = snapshot
    if snapshot_id:
        if not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise ResourceError("资源快照无效", 422)
        index = internal_path(db, user.id, project_id, f"{snapshot_id}.sqlite")
        with index_connection(index) as connection:
            summary = json.loads(connection.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()[0])
    with index_connection(index) as connection:
        row = connection.execute("SELECT * FROM entries WHERE id=? AND kind='file'", (value,)).fetchone()
    if row is None:
        raise ResourceError("文件不在资源清单中", 404)
    root = project_root(db, user, project_id)
    try:
        path = safe_regular_path(root, row["relative_path"])
    except (OSError, ValueError, RuntimeError) as exc:
        raise ResourceError("文件已变化或不可访问，请刷新资源", 404) from exc
    delivery = delivery_records(db, user, project_id).get(row["relative_path"])
    return path, entry_json(row, project, summary, delivery), row


def export_project_ids(task: Task) -> set[str]:
    payload = task.payload or {}
    references = (payload.get("scope") or {}).get("snapshots") or []
    return {item["project_id"] for item in references} | {item["resource_id"].split(":", 1)[0] for item in payload.get("files") or []} | {task.project_id}


def list_exports(db: Session, user: User) -> list[dict]:
    rows = db.execute(select(Task, TaskResult).join(TaskResult, TaskResult.task_id == Task.id).where(
        Task.owner_id == user.id, Task.task_type == "resources.package", Task.status == "succeeded",
    ).order_by(Task.finished_at.desc())).all()
    result = []
    live_ids = set(db.scalars(select(Project.id).where(Project.owner_id == user.id, Project.deleted_at.is_(None))).all())
    for task, record in rows:
        data = record.result
        try:
            path = internal_path(db, user.id, "exports", task.id, "files.zip")
            available = data.get("delivery_policy") == POLICY_VERSION and data.get("expires_at", "") > datetime.now(timezone.utc).isoformat() and path.is_file() and export_project_ids(task) <= live_ids
            result.append({
                "task_id": task.id, "name": data.get("export_name", "项目文件.zip"),
                "file_count": data.get("file_count", 0), "size_bytes": data.get("zip_bytes", 0),
                "expires_at": data.get("expires_at"), "available": available,
                "stored_bytes": path.stat().st_size if path.is_file() else 0,
                "unavailable_reason": None if available else ("旧包未经过成品校验，请重新下载成品合集" if data.get("delivery_policy") != POLICY_VERSION else "已过期或来源项目不可用"),
            })
        except ResourceError:
            continue
    return result
