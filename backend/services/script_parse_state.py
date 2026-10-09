"""Chapter-level state and version-bound submission for the script parse
workbench (「文本解析」页).

The page renders one row per split chapter (分册文件). Its state cannot come
from the generic task list: that window is "active + newest 200" with
supersede-hiding, so a 300-chapter project's older successful parses are
invisible there, and failed tasks carry no ``result`` to identify the file
from. This service therefore aggregates the answer directly from the task
table (every script.parse task of the project, no window, no hiding) plus
the file table (current input digest) and the result artifacts.

Layering: ``api/script_parse`` -> this service -> platform.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from sqlalchemy import Select, func, insert, or_, select, update
from sqlalchemy.orm import Session

from ..core.config import get_config
from ..engines.book import decode_buffer
from ..engines.script import fix_mojibake
from ..engines.script_prompts import load_default_prompts
from ..platform.models import Project, ProjectFile, Task, TaskResult, TextFormatFlow, User, new_id, utcnow
from ..platform.storage import (
    configured_storage_root,
    object_path,
    project_workspace_path,
    project_directory_key,
    sha256_file,
)
from ..core.filenames import filename_aliases, legacy_storage_name
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..platform.task_submission import TaskSubmissionError
from ..platform.batch_submission import submit_task_batch
from ..core.request_context import bind_workspace, reset_workspace
from .list_paging import page_meta, page_slice
from .task_operations import owned_project
from .text_format_workbench import FLOW_TASK_TYPES, _version_from_flow

_SPLIT_MODULE = "02_split_text"
_PARSED_MODULE = "03_parsed_json"
_PARSE_TASK_TYPE = "script.parse"


class ScriptParseError(Exception):
    def __init__(self, status_code: int, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.detail = detail or {}


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _module_of(object_key: str) -> str | None:
    parts = object_key.split("/")
    return parts[2] if len(parts) >= 4 else None


def _text_format_busy(db: Session, owner_id: str, project_id: str) -> bool:
    return (
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.owner_id == owner_id,
                Task.project_id == project_id,
                Task.task_type.in_(list(FLOW_TASK_TYPES)),
                Task.status.in_(sorted(ACTIVE_TASK_STATUSES)),
            )
        )
        or 0
    ) > 0


def _file_row(db: Session, user: User, project_id: str, file_id: str) -> ProjectFile | None:
    return db.scalar(
        select(ProjectFile).where(
            ProjectFile.id == file_id,
            ProjectFile.project_id == project_id,
            ProjectFile.owner_id == user.id,
            ProjectFile.deleted_at.is_(None),
        )
    )


def _split_files(db: Session, user: User, project_id: str) -> dict[str, ProjectFile]:
    """The project's current split files, keyed by file name. The file table is
    the source of truth (its sha256 is the CURRENT content digest — republish
    upserts the row); a name maps to at most one live row. The split module is
    filtered in SQL so other modules' rows never materialize."""
    rows = db.scalars(
        select(ProjectFile).where(
            ProjectFile.project_id == project_id,
            ProjectFile.owner_id == user.id,
            ProjectFile.deleted_at.is_(None),
            ProjectFile.object_key.like(f"%/{_SPLIT_MODULE}/%"),
        )
    ).all()
    return {
        item.original_name: item
        for item in rows
        if item.original_name
    }


def _resolve_split_name(rows: dict[str, ProjectFile], name: str) -> ProjectFile | None:
    if name in rows:
        return rows[name]
    spellings = set(filename_aliases(name))
    matches = {item.id: item for key, item in rows.items()
               if key in spellings or Path(item.object_key).name in spellings}
    return next(iter(matches.values())) if len(matches) == 1 else None


def _catalog_split_file(db: Session, user: User, project: Project, path: Path) -> ProjectFile:
    """Register a legacy split file that predates the file table.

    Pre-workbench data lives on disk with no ProjectFile row, hence no
    digest; cataloging here gives the version-bound submit a digest to
    work with. Mirrors ``catalog_managed_file`` but stays scoped to THIS
    project instead of the active-workspace pointer (the anti-pattern this
    contract replaces)."""
    object_key = (
        f"{project_directory_key(db, user.username, project.id)}/{_SPLIT_MODULE}/{path.name}"
    )
    values = {
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    item = db.scalar(
        select(ProjectFile).where(
            ProjectFile.object_key == object_key,
            ProjectFile.project_id == project.id,
            ProjectFile.owner_id == user.id,
        )
    )
    if item is None:
        item = ProjectFile(
            project_id=project.id,
            owner_id=user.id,
            object_key=object_key,
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            kind="legacy",
            original_name=path.name,
            **values,
        )
        db.add(item)
    else:
        # 已有行的 original_name 可能是引擎原始名（legacy/历史数据，版本列表
        # 与排版页仍按它引用）；补登只更新摘要，绝不能改写名字——改写会让
        # 同一文件的所有旧名字引用变成「分册文本不存在」。
        for key, value in values.items():
            setattr(item, key, value)
        item.deleted_at = None
    return item


def _catalog_split_files(db, user, project, paths):
    """Catalogue legacy selections with bounded SQL inside batch admission."""
    if not paths:
        return {}
    prefix = project_directory_key(db, user.username, project.id) + "/" + _SPLIT_MODULE + "/"
    candidates = {prefix + path.name: path for path in paths}
    old = {item.object_key: item for item in db.scalars(select(ProjectFile).where(
        ProjectFile.project_id == project.id, ProjectFile.owner_id == user.id,
        ProjectFile.object_key.in_(candidates)))}
    now, additions, changes = utcnow(), [], []
    for key, path in candidates.items():
        values = {"size_bytes": path.stat().st_size, "sha256": sha256_file(path), "deleted_at": None}
        if key in old:
            changes.append({"id": old[key].id, **values})
        else:
            additions.append({"id": new_id(), "owner_id": user.id, "project_id": project.id,
                "object_key": key, "original_name": path.name, "kind": "legacy",
                "content_type": "text/plain", "created_at": now, **values})
    for offset in range(0, len(additions), 100):
        db.execute(insert(ProjectFile), additions[offset:offset + 100])
    for offset in range(0, len(changes), 100):
        db.execute(update(ProjectFile), changes[offset:offset + 100])
    rows = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project.id,
        ProjectFile.owner_id == user.id, ProjectFile.object_key.in_(candidates)).execution_options(populate_existing=True))
    return {Path(item.object_key).name: item for item in rows}


def _source_section(db: Session, user: User, project: Project) -> dict:
    """Chapter/file listing: prefer the latest workbench version (read-only —
    no adopt / no flow advance); fall back to legacy split files when no ready
    version exists (pre-workbench data that never had a book.split task)."""
    flow = db.scalar(
        select(TextFormatFlow)
        .where(TextFormatFlow.project_id == project.id, TextFormatFlow.owner_id == user.id)
        .order_by(TextFormatFlow.updated_at.desc(), TextFormatFlow.id.desc())
        .limit(1)
    )
    version = None
    if flow is not None and flow.status == "ready":
        version = _version_from_flow(db, user, project, flow)
    if version is not None:
        return {
            "mode": "version",
            "version": {
                "flow_id": version["flow_id"],
                "version_status": version["version_status"],
                "chapters": [
                    {
                        "key": c.get("key"),
                        "seq": c.get("seq"),
                        "numStr": c.get("numStr") or "",
                        "title": c.get("title") or "",
                        "chars": c.get("chars") or 0,
                    }
                    for c in version.get("chapters") or []
                ],
                "files": [
                    {"file_id": f.get("file_id"), "name": f.get("name"), "chars": f.get("chars") or 0}
                    for f in version.get("files") or []
                ],
            },
        }
    # Legacy: no (ready) version. Enumerate the project's split files — file
    # table first (it carries the digests), disk as best-effort fallback.
    split_rows = _split_files(db, user, project.id)
    disk_dir = project_workspace_path(db, user.username, project.id) / _SPLIT_MODULE
    disk_names = (
        [path.name for path in sorted(disk_dir.iterdir()) if path.is_file() and path.name.endswith(".txt")]
        if disk_dir.is_dir()
        else []
    )
    if not split_rows and not disk_names:
        return {"mode": "empty"}
    # Recorded paths win. Only missing original spellings use legacy disk aliases.
    known_disk_names = set(split_rows) | {Path(item.object_key).name for item in split_rows.values()}
    for name, item in split_rows.items():
        if Path(item.object_key).name not in disk_names:
            known_disk_names.add(legacy_storage_name(name))
    return {
        "mode": "legacy",
        "legacy_files": [
            {
                "name": name,
                "file_id": item.id,
                "sha256": item.sha256,
                "size": item.size_bytes,
            }
            for name, item in sorted(split_rows.items())
        ],
        "disk_only": [name for name in disk_names if name not in known_disk_names],
    }


def _parse_tasks(db: Session, owner_id: str, project_id: str, names: list[str] | None = None, input_ids: list[str] | None = None) -> list[SimpleNamespace]:
    """Materialize only the tasks state actually needs: per normalized input
    identity, the latest parse task and the latest succeeded task (SQL
    windowing). Twenty rounds of history per chapter no longer reach this
    process; results are still loaded only for the surviving task ids.

    Identity prefers the recorded file row and falls back to the raw name
    spelling, so legacy alias spellings keep working — they merge per resolved
    row in ``_build_file_states``."""
    payload_name = Task.payload["source_name"].as_string()
    payload_input = Task.payload["input_file_id"].as_string()
    identity = func.coalesce(func.nullif(payload_input, ""), payload_name, "").label("identity")
    where = [Task.owner_id == owner_id, Task.project_id == project_id, Task.task_type == _PARSE_TASK_TYPE]
    if names is not None:
        where.append(or_(payload_name.in_([alias for name in names for alias in filename_aliases(name)]),
                         payload_input.in_(input_ids or [])))
    base = select(Task.id, Task.status, Task.progress, Task.error_message,
                  Task.created_at, Task.finished_at,
                  payload_name.label("source_name"), payload_input.label("input_file_id"),
                  identity).where(*where).subquery()

    def ranked(extra: list) -> Select:
        source = select(base.c).where(*extra).subquery()
        window = select(source.c,
            func.row_number().over(partition_by=[source.c.identity],
                                   order_by=[source.c.created_at.desc(), source.c.id.desc()]).label("rn")
        ).subquery()
        return select(window.c).where(window.c.rn == 1)

    rows = db.execute(ranked([]).union_all(ranked([base.c.status == "succeeded"]))).all()
    tasks = [SimpleNamespace(
        id=row.id, status=row.status, progress=row.progress,
        error_message=row.error_message, created_at=row.created_at, finished_at=row.finished_at,
        payload={"source_name": row.source_name, "input_file_id": row.input_file_id})
        for row in rows]
    # A task can be both latest and latest-succeeded; restore the historical
    # "newest wins" order across identities before the dedupe pass.
    ordered = sorted(tasks, key=lambda task: (task.created_at, task.id), reverse=True)
    deduped: dict[str, SimpleNamespace] = {}
    for task in ordered:
        deduped.setdefault(task.id, task)
    return list(deduped.values())


def _active_parse_names(db: Session, owner_id: str, project_id: str) -> set[str]:
    """File names with a script.parse task still in flight (any active
    status). Only active rows are loaded, so this stays cheap regardless of
    how many historical attempts the project has."""
    payloads = db.scalars(
        select(Task.payload).where(
            Task.owner_id == owner_id,
            Task.project_id == project_id,
            Task.task_type == _PARSE_TASK_TYPE,
            Task.status.in_(sorted(ACTIVE_TASK_STATUSES)),
        )
    ).all()
    return {
        name
        for name in (str((payload or {}).get("source_name") or "") for payload in payloads)
        if name
    }


def _latest_success_results(db: Session, task_ids: list[str]) -> dict[str, dict]:
    """Scalars extracted from succeeded tasks' result envelopes (JSON item
    extraction — the full entries array is never materialized). One query for
    the whole batch (review [P3]: the per-task version was an N+1 — a
    300-chapter project meant 300+ queries per state aggregate). The envelope
    carries the source digest; the artifact's own digest comes from the file
    table, so it is deliberately NOT read from here. Pre-fingerprint
    envelopes (pre-workbench tasks) lack ``source_sha256`` but keep
    ``source_file_id`` / ``input_chars`` — the row that row points to and the
    on-disk input length are how those results are re-verified
    (see ``_legacy_result_status``)."""
    if not task_ids:
        return {}
    rows = db.execute(
        select(
            TaskResult.task_id,
            TaskResult.result["source_sha256"].as_string(),
            TaskResult.result["source_file_id"].as_string(),
            TaskResult.result["input_chars"].as_integer(),
            TaskResult.result["name"].as_string(),
            TaskResult.result["file_id"].as_string(),
        )
        .where(TaskResult.task_id.in_(task_ids))
    ).all()
    out: dict[str, dict] = {}
    for task_id, source_sha, source_file_id, input_chars, name, file_id in rows:
        out[task_id] = {
            "source_sha256": source_sha,
            "source_file_id": source_file_id,
            "input_chars": input_chars,
            "name": name,
            "file_id": file_id,
        }
    return out


def _input_char_count(path: Path) -> int | None:
    """The engine's ``input_chars`` recomputed for the CURRENT on-disk file
    (decode → strip → mojibake fix, same pipeline as the parse engine)."""
    try:
        text, _enc = decode_buffer(path.read_bytes())
    except OSError:
        return None
    return len(fix_mojibake((text or "").strip()))


def _legacy_result_status(
    storage_root: Path, split_rows_by_id: dict[str, ProjectFile], meta: dict, task: Task
) -> str:
    """Re-verify a pre-fingerprint success (envelope carries no input digest).

    book.split republish RESURRECTS the split-file row in place (clears
    ``deleted_at``, updates the sha on the same row), so "the input row is
    still live" does NOT prove the content is unchanged. Instead compare the
    envelope's ``input_chars`` — the length of the engine-processed input text
    at parse time — against the same computation on the current file on disk:
    mismatch means the input was republished (stale), a match makes the old
    result usable, and anything that cannot be computed stays 待确认."""
    row = split_rows_by_id.get(str(meta.get("source_file_id") or ""))
    if row is None or task.finished_at is None or row.created_at is None:
        return "unverified"
    if row.created_at > task.finished_at:
        return "unverified"
    expected = meta.get("input_chars")
    if not isinstance(expected, int) or expected < 0 or not row.object_key:
        return "unverified"
    actual = _input_char_count(object_path(row.object_key, storage_root))
    if actual is None:
        return "unverified"
    return "usable" if actual == expected else "stale"


def _build_file_states(
    db: Session, user: User, project: Project, names: list[str]
) -> list[dict]:
    """Per-file parse state: latest task (any status) + latest successful
    result, judged against the CURRENT input digest. Task execution state and
    result availability are separate axes: a failed re-parse with an older
    still-valid result reports both (status failed + result usable)."""
    split_rows = _split_files(db, user, project.id)
    tasks = _parse_tasks(db, user.id, project.id, names, [item.id for name in names if (item := _resolve_split_name(split_rows, name)) is not None])
    split_rows_by_id = {item.id: item for item in split_rows.values()}
    latest_by_id: dict[str, Task] = {}
    success_by_id: dict[str, Task] = {}
    latest_by_name: dict[str, Task] = {}
    success_by_name: dict[str, Task] = {}
    for task in tasks:
        payload = task.payload or {}
        name = str(payload.get("source_name") or "")
        input_id = str(payload.get("input_file_id") or "")
        item = split_rows_by_id.get(input_id)
        if item is None:
            item = _resolve_split_name(split_rows, name)
        if item is not None:
            latest_by_id.setdefault(item.id, task)
            if task.status == "succeeded":
                success_by_id.setdefault(item.id, task)
        if name:
            latest_by_name.setdefault(name, task)
            if task.status == "succeeded":
                success_by_name.setdefault(name, task)
    storage_root = configured_storage_root(db)

    success_lookup = _latest_success_results(db, [task.id for task in tasks if task.status == "succeeded"])
    success_meta: dict[str, dict | None] = {
        task.id: success_lookup.get(task.id) for task in tasks if task.status == "succeeded"
    }
    # Result artifacts must still exist in the file table (republish keeps the
    # row alive; a deleted / never-catalogued artifact fails verification).
    artifact_ids = {m["file_id"] for m in success_meta.values() if m and m.get("file_id")}
    artifact_by_id = {}
    if artifact_ids:
        for item in db.scalars(select(ProjectFile).where(ProjectFile.id.in_(artifact_ids))):
            if item.deleted_at is None:
                artifact_by_id[item.id] = item

    states: list[dict] = []
    for name in names:
        input_item = _resolve_split_name(split_rows, name)
        input_sha = input_item.sha256 if input_item is not None else None

        task = latest_by_id.get(input_item.id) if input_item is not None else latest_by_name.get(name)
        latest = None
        if task is not None:
            latest = {
                "id": task.id,
                "status": task.status,
                "progress": round(task.progress / 100.0, 4),
                "error": task.error_message or "",
                "created_at": _iso(task.created_at),
                "finished_at": _iso(task.finished_at),
            }

        success = success_by_id.get(input_item.id) if input_item is not None else success_by_name.get(name)
        result = None
        result_status: str | None = None
        if success is not None:
            meta = success_meta[success.id]
            if meta is not None:
                artifact = artifact_by_id.get(meta.get("file_id") or "")
                verified = artifact is not None
                result = {
                    "task_id": success.id,
                    "name": meta.get("name"),
                    "file_id": meta.get("file_id"),
                    "sha256": artifact.sha256 if verified else None,
                    "size": artifact.size_bytes if verified else None,
                    "source_sha256": meta.get("source_sha256"),
                    "finished_at": _iso(success.finished_at),
                    "verified": verified,
                }
                if not verified:
                    result_status = "unverified"
                elif meta.get("source_sha256") and input_sha:
                    if meta["source_sha256"] == input_sha:
                        result_status = "usable"
                    else:
                        result_status = "stale"
                else:
                    # Pre-fingerprint success (or one-sided digest): no input
                    # digest to compare — re-verify against the current input
                    # (usable / stale / unverified 待确认, never 完成 by default).
                    result_status = _legacy_result_status(
                        storage_root, split_rows_by_id, meta, success
                    )

        states.append(
            {
                "name": name,
                "input": _file_lite(input_item) if input_item is not None else None,
                "latest_task": latest,
                "result": result,
                "result_status": result_status,
            }
        )
    return states


def _file_lite(item: ProjectFile) -> dict:
    return {
        "file_id": item.id,
        "name": item.original_name,
        "sha256": item.sha256,
        "size": item.size_bytes,
    }


def get_state(db: Session, user: User, project_id: str, *, page: int | None = None, page_size: int = 10, query: str = "", filter: str = "all", keys_only: bool = False, summary_only: bool = False) -> dict:
    project = owned_project(db, user.id, project_id)
    if project is None:
        raise ScriptParseError(404, "项目不存在")
    source = _source_section(db, user, project)
    if source["mode"] == "version":
        names = [f["name"] for f in source["version"]["files"] if f.get("name")]
    elif source["mode"] == "legacy":
        # 磁盘有、文件表无记录（未登记历史数据）的文件也在内：它们可以解析，
        # 但没有摘要，旧结果不可判（待确认）。版本模式下绝不允许磁盘杂文件混入。
        names = [f["name"] for f in source["legacy_files"]] + list(source.get("disk_only") or [])
    else:
        names = []
    if summary_only:
        # Independent read model: exact whole-book controls, never full row bodies.
        states = _build_file_states(db, user, project, names)
        active = [(item.get("latest_task") or {}) for item in states
                  if (item.get("latest_task") or {}).get("status") in ACTIVE_TASK_STATUSES]
        done = sum(item.get("result_status") == "usable" and
                   (item.get("latest_task") or {}).get("status") not in ACTIVE_TASK_STATUSES | {"failed", "timeout", "cancelled"}
                   for item in states)
        return {"total": len(names), "done_count": done, "active_task_ids": list(dict.fromkeys(t["id"] for t in active))}
    if page is None and not query and filter == "all" and not keys_only:
        return {"source": source, "text_format_busy": _text_format_busy(db, user.id, project.id),
                "files": _build_file_states(db, user, project, names)}
    refs = []
    if source["mode"] == "version":
        files = source["version"]["files"]
        refs = [{**c, "name": files[c.get("seq", 1) - 1]["name"]} for c in source["version"]["chapters"]
                if 0 < c.get("seq", 1) <= len(files)]
    else:
        refs = [{"name": name, "seq": i + 1, "title": Path(name).stem, "numStr": str(i + 1), "chars": 0} for i, name in enumerate(names)]
    q = query.strip().casefold()
    matching = [c for c in refs if not q or q in c["name"].casefold() or q in c.get("title", "").casefold()
                or q in str(c.get("seq", "")) or q in str(c.get("numStr", ""))]
    states = None
    if filter != "all":
        states = _build_file_states(db, user, project, [c["name"] for c in matching])
        def matches(item):
            active = (item.get("latest_task") or {}).get("status") in ACTIVE_TASK_STATUSES
            done = not active and item.get("result_status") == "usable" and (item.get("latest_task") or {}).get("status") not in {"failed", "timeout", "cancelled"}
            if filter == "done":
                return done
            if filter == "failed":
                return not active and (item.get("latest_task") or {}).get("status") in {"failed", "timeout"}
            return not done
        eligible = {item["name"] for item in states if matches(item)}
        matching = [c for c in matching if c["name"] in eligible]
    if keys_only:
        # Explicit bulk selection may fetch lightweight input references, never
        # task/results bodies or previews for the whole book.
        states = states or _build_file_states(db, user, project, [c["name"] for c in matching])
        matching_names = {c["name"] for c in matching}
        return {"items": [{"name": item["name"], "input": item["input"], "result_status": item.get("result_status"), "status": (item.get("latest_task") or {}).get("status")} for item in states
                          if item["name"] in matching_names and (item.get("latest_task") or {}).get("status") not in ACTIVE_TASK_STATUSES]}
    visible = page_slice(matching, page, page_size) if page is not None else matching
    visible_names = [c["name"] for c in visible]
    if states is None:
        states = _build_file_states(db, user, project, visible_names)
    else:
        states = [item for item in states if item["name"] in set(visible_names)]
    if page is not None:
        source = {**source}
        if source["mode"] == "version":
            source["version"] = {**source["version"], "chapters": visible,
                                 "files": [f for f in source["version"]["files"] if f["name"] in set(visible_names)]}
        elif source["mode"] == "legacy":
            source["legacy_files"] = [f for f in source["legacy_files"] if f["name"] in set(visible_names)]
            source["disk_only"] = [n for n in source["disk_only"] if n in set(visible_names)]
    return {
        "source": source, "text_format_busy": _text_format_busy(db, user.id, project.id), "files": states,
        **({"chapter_refs": visible, "pagination": page_meta(len(matching), page, page_size, {"all": len(refs)})} if page is not None else {}),
    }



def _resolved_prompts():
    """Configured prompts with empty fields filled from bundled defaults —
    the task's config snapshot must never ship an empty prompt template."""
    prompts = get_config().prompts
    system_prompt, user_prompt = load_default_prompts()
    return prompts.model_copy(update={
        "system_prompt": prompts.system_prompt or system_prompt,
        "user_prompt": prompts.user_prompt or user_prompt,
    })


def submit_run(
    db: Session,
    user: User,
    project_id: str,
    files: list[dict],
    checks: dict | None,
    idempotency_key: str | None = None,
) -> dict:
    if not 1 <= len(files) <= 1000:
        raise ScriptParseError(422, "一次请选择 1 至 1000 个章节。")
    def prepare():
        token = bind_workspace(project_workspace_path(db, user.username, project_id))
        try:
            return _prepare_run(db, user, project_id, files, checks)
        finally:
            reset_workspace(token)
    try:
        return submit_task_batch(db=db, user=user, project_id=project_id, task_type=_PARSE_TASK_TYPE,
            request={"files": files, "checks": checks}, prepare=prepare,
            idempotency_key=idempotency_key, receipt_field="files")
    except TaskSubmissionError as exc:
        raise ScriptParseError(exc.status_code, exc.message) from exc


def _prepare_run(db, user, project_id, files, checks):
    """Version-bound batch submit: resolve every file against the project's
    file table, verify caller-supplied digests, then submit one parse task per
    file. Digest mismatch / split publishing / an in-flight parse of the same
    file are all 409s with a machine-readable list, so the page can refresh
    and re-ask the user instead of submitting stale selections."""
    project = owned_project(db, user.id, project_id)
    if project is None:
        raise ScriptParseError(404, "项目不存在")
    # The outer batch admission holds the project lock until every row commits.
    if _text_format_busy(db, user.id, project.id):
        raise ScriptParseError(409, "排版与分册任务正在进行，暂不能提交解析")

    split_rows = _split_files(db, user, project.id)
    # Legacy disk files without a table row get catalogued on submit — the
    # 历史文件兼容入口: visible in state, submittable, digest computed now.
    workspace = project_workspace_path(db, user.username, project.id)
    disk_dir = workspace / _SPLIT_MODULE
    legacy_paths = []
    if disk_dir.is_dir():
        for name in set(split_rows) | {str(f.get("name") or "") for f in files}:
            if name and name not in split_rows:
                candidate = (disk_dir / Path(name).name).resolve()
                try:
                    candidate.relative_to(disk_dir.resolve())
                    candidate.relative_to(workspace.resolve())
                except ValueError:
                    continue
                if candidate.name.endswith(".txt") and candidate.is_file():
                    legacy_paths.append(candidate)
    split_rows.update(_catalog_split_files(db, user, project, legacy_paths))
    db.flush()

    wanted: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for entry in files:
        name = str(entry.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        wanted.append((name, entry.get("sha256")))
    if not wanted:
        raise ScriptParseError(422, "请选择要解析的章节。")

    # Resolve against recorded disk paths; never equate two new punctuation names.
    for name, _sha in wanted:
        if name not in split_rows:
            item = _resolve_split_name(split_rows, name)
            if item is not None:
                split_rows[name] = item

    missing = [name for name, _ in wanted if name not in split_rows]
    if missing:
        raise ScriptParseError(409, "以下分册文本不存在（可能已被重新分册覆盖）：" + "、".join(missing[:5]))

    changed = [
        name
        for name, sha in wanted
        if sha and split_rows[name].sha256 and split_rows[name].sha256 != sha
    ]
    if changed:
        raise ScriptParseError(409, "分册文本已变更，请刷新后重新选择。", {"changed": changed})

    # 同一文件可能以别名（原始名/磁盘名）同批出现：按行身份去重、保留首个，
    # 避免同一 input_file_id 双任务；在途判定同样按清洗名对齐，别名不绕过 409。
    seen_item_ids: set[str] = set()
    deduped: list[tuple[str, str | None]] = []
    for name, sha in wanted:
        item = split_rows[name]
        if item.id in seen_item_ids:
            continue
        seen_item_ids.add(item.id)
        deduped.append((name, sha))
    wanted = deduped

    active = _active_parse_names(db, user.id, project.id)
    active_ids = {item.id for name in active
                  if (item := _resolve_split_name(split_rows, name)) is not None}
    in_flight = [name for name, _sha in wanted if split_rows[name].id in active_ids or name in active]
    if in_flight:
        raise ScriptParseError(409, "以下章节已有解析任务在进行：" + "、".join(sorted(in_flight)[:5]))

    cfg = get_config()
    prompts = _resolved_prompts()
    snapshot: dict[str, Any] = {
        "llm": cfg.llm.model_dump(mode="json"),
        "prompts": prompts.model_dump(mode="json"),
        "generation": cfg.generation.model_dump(mode="json"),
    }
    if checks:
        # 用户勾选的检查开关 = 每任务权威值（同 legacy 端点语义）。
        snapshot["generation"].update(checks)

    entries: list[dict] = []
    for name, _sha in wanted:
        item = split_rows[name]
        entries.append({"label": f"文本解析 · {name}",
            "payload": {"input_file_id": item.id, "source_name": item.original_name,
                        "input_sha256": item.sha256},
            "receipt": {"name": name, "input_sha256": item.sha256}})
    return entries, snapshot


def result_file(db: Session, user: User, project_id: str, file_id: str) -> tuple[ProjectFile, Path]:
    """The parse-result artifact behind a file id: must belong to this
    project, live in 03_parsed_json, and be a .json the page can render."""
    project = owned_project(db, user.id, project_id)
    if project is None:
        raise ScriptParseError(404, "项目不存在")
    item = _file_row(db, user, project.id, file_id)
    if item is None:
        raise ScriptParseError(404, "解析结果不存在")
    if _module_of(item.object_key) != _PARSED_MODULE or not item.original_name.endswith(".json"):
        raise ScriptParseError(404, "指定的文件不是解析结果")
    path = object_path(item.object_key, configured_storage_root(db))
    if not path.is_file():
        raise ScriptParseError(409, "解析结果内容已丢失，请刷新后重试")
    return item, path
