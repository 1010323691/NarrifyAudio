"""Worker operations for inventory, streaming packages and qualified temp cleanup."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from ..core.safe_filesystem import file_identity, identity_matches, is_link_or_junction, safe_regular_path, matches_open_file_identity
from .database import SessionLocal
from .models import Project, Task, User
from .resource_inventory import (
    AUDIO_EXTENSIONS, CACHE_MODULES, EXPORT_RETENTION_SECONDS, ResourceError,
    _filters, build_index, current_snapshot, index_connection, internal_path,
    owned_project, project_root, resolve_resource, source_version, entry_json, attach_deliveries,
)
from .storage import safe_display_name, safe_project_workspace_path, sha256_file
from .task_context import EngineExecutionContext, update_progress
from .task_contracts import TaskClaim, TaskExecutionError, TaskOutcome, TaskSideEffectOutput
from .task_lifecycle import ACTIVE_TASK_STATUSES
from .resource_delivery import POLICY_VERSION, delivery_records


def _outcome(claim: TaskClaim, metadata: dict, outputs: list[TaskSideEffectOutput] | None = None) -> TaskOutcome:
    with SessionLocal() as db:
        path = internal_path(db, claim.owner_id, "attempts", claim.attempt_id, "result.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return TaskOutcome(
        temp_path=path, output_name="resource-result.json", content_type="application/json",
        size_bytes=path.stat().st_size, sha256=sha256_file(path), metadata=metadata,
        side_effect_outputs=tuple(outputs or []), result_only=True,
    )


def _execute_resource_scan(claim: TaskClaim) -> TaskOutcome:
    context = EngineExecutionContext(claim)
    outputs: list[TaskSideEffectOutput] = []
    staged_paths: list[Path] = []
    summaries = []
    scan_errors = []
    checks = 0
    last_check = 0.0

    def check():
        nonlocal checks, last_check
        checks += 1
        now = time.monotonic()
        if checks % 200 == 0 or now - last_check >= 0.25:
            context.check()
            last_check = now

    try:
        with SessionLocal() as db:
            user = db.get(User, claim.owner_id)
            if user is None:
                raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
            owned_project(db, user, claim.project_id)
            ids = list(dict.fromkeys(claim.payload.get("project_ids") or [claim.project_id]))
            projects = []
            for project_id in ids:
                projects.append(owned_project(db, user, project_id))
            if claim.payload.get("include_trash"):
                projects.extend(db.scalars(select(Project).where(
                    Project.owner_id == user.id, Project.deleted_at.is_not(None),
                )).all())
            targets = [(project.id, project.name, safe_project_workspace_path(db, user.username, project.id), source_version(db, user.id, project.id)) for project in projects]
        for index, (project_id, name, root, version) in enumerate(targets):
            context.check()
            if root is None or not root.is_dir() or is_link_or_junction(root):
                scan_errors.append({"project_id": project_id, "message": "项目存储目录不存在或不可安全访问"})
                continue
            with SessionLocal() as db:
                staged = internal_path(db, claim.owner_id, "attempts", claim.attempt_id, f"{project_id}.sqlite")
            staged_paths.append(staged)
            summary = build_index(root, staged, project_id=project_id, version=version, check=check,
                                  progress=lambda count: update_progress(claim, min(89, int(index * 90 / max(1, len(targets)))), f"正在读取「{name}」：{count:,}个文件"))
            with SessionLocal() as db:
                final = internal_path(db, claim.owner_id, project_id, f"{summary['snapshot_id']}.sqlite")
                pointer = internal_path(db, claim.owner_id, "attempts", claim.attempt_id, f"{project_id}.json")
                pointer_final = internal_path(db, claim.owner_id, project_id, "current.json")
                previous = current_snapshot(db, claim.owner_id, project_id)
            if not summary["complete"] and previous is not None and previous[1]["complete"]:
                outputs.append(TaskSideEffectOutput(staged, final))
                summaries.append({"project_id": project_id, **summary})
                scan_errors.append({"project_id": project_id, "message": "部分目录无法读取，保留上次完整清单。请检查读取权限后刷新。"})
                continue
            staged_paths.append(pointer)
            pointer.write_text(json.dumps({"snapshot_id": summary["snapshot_id"]}), encoding="utf-8")
            outputs.extend([TaskSideEffectOutput(staged, final), TaskSideEffectOutput(pointer, pointer_final)])
            summaries.append({"project_id": project_id, **summary})
        context.check()
        update_progress(claim, 95, "清单读取完成，正在发布资源快照")
        return _outcome(claim, {"snapshots": summaries, "scan_errors": scan_errors}, outputs)
    except BaseException:
        for staged in staged_paths:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def package_selection(db, user: User, payload: dict, check=lambda: None) -> list[tuple[Path, dict, sqlite3.Row]]:
    """Resolve a frozen selection; query exports refer to immutable snapshot ids."""
    selected = payload.get("files")
    result = []
    if selected:
        for item in selected:
            check()
            result.append(resolve_resource(db, user, item["resource_id"], item["snapshot_id"]))
    else:
        scope = payload["scope"]
        snapshots = scope["snapshots"]
        where, values = _filters(scope.get("category", "all"), scope.get("path", ""), scope.get("query", ""), scope.get("extension", ""), directory_mode=False)
        for reference in snapshots:
            project = owned_project(db, user, reference["project_id"])
            index = internal_path(db, user.id, project.id, f"{reference['snapshot_id']}.sqlite")
            with index_connection(index) as connection:
                scope_where = where
                if scope.get("category") == "deliverables":
                    attach_deliveries(connection, delivery_records(db, user, project.id))
                    scope_where += " AND relative_path IN (SELECT relative_path FROM eligible_deliveries)"
                summary = json.loads(connection.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()[0])
                if not summary["complete"]:
                    raise ResourceError("资源清单读取不完整，请刷新后打包全部结果")
                # Only descriptors are collected, never file bytes. Sources are
                # revalidated again while streaming into the archive.
                root = project_root(db, user, project.id)
                for row in connection.execute(f"SELECT * FROM entries WHERE kind='file' AND {scope_where} ORDER BY sort_key", values):
                    check()
                    result.append((safe_regular_path(root, row['relative_path']), entry_json(row, project, summary), row))
    unique = {entry[1]["id"]: entry for entry in result}
    policies = {}
    for path, description, _record in unique.values():
        project_id = description["project_id"]
        if project_id not in policies:
            policies[project_id] = delivery_records(db, user, project_id)
        if description["relative_path"] not in policies[project_id]:
            raise ResourceError("此资源为制作资料或成品已变化，不提供下载或打包，请刷新成品清单。", 403)
        authority = policies[project_id][description["relative_path"]]
        description["_delivery_task_id"] = authority["task_id"]
        description["_delivery_identity"] = authority["identity"]
    if not unique:
        raise ResourceError("没有可打包的文件", 422)
    return list(unique.values())


def _execute_resource_package(claim: TaskClaim) -> TaskOutcome:
    context = EngineExecutionContext(claim)
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        if user is None:
            raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
        last_check = 0.0
        def check_selection():
            nonlocal last_check
            if time.monotonic() - last_check >= .25:
                context.check()
                last_check = time.monotonic()
        selected = package_selection(db, user, claim.payload, check_selection)
        if claim.payload.get("delivery_count") is not None and len(selected) != claim.payload["delivery_count"]:
            raise ResourceError("成品范围已变化，请刷新后重新打包")
        staged = internal_path(db, user.id, "attempts", claim.attempt_id, "files.zip")
        final = internal_path(db, user.id, "exports", claim.task_id, "files.zip")
    staged.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = sum(item[1]["size_bytes"] for item in selected)
    multiple_projects = len({item[1]["project_id"] for item in selected}) > 1
    copied = 0
    names: set[str] = set()
    try:
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for index, (path, description, record) in enumerate(selected):
                context.check()
                before = path.stat()
                expected = (record["size_bytes"], record["mtime_ns"], record["ctime_ns"], int(record["device"]), int(record["inode"]))
                if file_identity(before) != expected:
                    raise ResourceError(f"文件已变化，请刷新后重试：{description['relative_path']}")
                with SessionLocal() as db:
                    current_user = db.get(User, claim.owner_id)
                    checked, current_description, _ = resolve_resource(db, current_user, description["id"], description["snapshot_id"])
                    if not current_description["can_package"]:
                        raise ResourceError("成品资格已变化，请重新选择成品", 403)
                if checked != path:
                    raise ResourceError("文件路径已变化，请刷新后重试")
                name = description["relative_path"]
                if multiple_projects:
                    name = f"{safe_display_name(description['project_name'])}__{description['project_id'][:8]}/{name}"
                if name.casefold() in names:
                    raise ResourceError("打包文件路径存在冲突")
                names.add(name.casefold())
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_STORED if path.suffix.lower() in AUDIO_EXTENSIONS | {".zip", ".png", ".jpg", ".jpeg", ".webp"} else zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                info.date_time = datetime.fromtimestamp(max(315532800, min(before.st_mtime, 4354819199))).timetuple()[:6]
                with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as output:
                    if not matches_open_file_identity(os.fstat(source.fileno()), expected):
                        raise ResourceError(f"文件已变化：{description['relative_path']}")
                    while chunk := source.read(1024 * 1024):
                        context.check()
                        output.write(chunk)
                        copied += len(chunk)
                    if not matches_open_file_identity(os.fstat(source.fileno()), expected) or file_identity(path.stat()) != expected:
                        raise ResourceError(f"打包期间文件变化：{description['relative_path']}")
                update_progress(claim, min(95, int(copied * 95 / max(1, total_bytes))), f"打包 {index + 1}/{len(selected)}：{description['name']}")
        context.check()
        # A qualification can be revoked without changing the audio bytes.
        # Recheck every selected source once more after the entire copy phase.
        with SessionLocal() as db:
            current_user = db.get(User, claim.owner_id)
            groups = {}
            for _path, description, _record in selected:
                groups.setdefault(description["project_id"], []).append(description)
            for project_id, descriptions in groups.items():
                current = delivery_records(db, current_user, project_id, [item["relative_path"] for item in descriptions])
                for item in descriptions:
                    authority = current.get(item["relative_path"])
                    if not authority or authority["task_id"] != item["_delivery_task_id"] or not identity_matches(authority["identity"], item["_delivery_identity"]):
                        raise ResourceError("成品资格在打包期间已变化，请重新选择", 403)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=EXPORT_RETENTION_SECONDS)
        return _outcome(claim, {
            "delivery_policy": POLICY_VERSION,
            "export_name": safe_display_name(claim.payload.get("name") or "项目文件") + ".zip",
            "file_count": len(selected), "source_bytes": total_bytes,
            "zip_bytes": staged.stat().st_size, "expires_at": expires_at.isoformat(),
        }, [TaskSideEffectOutput(staged, final)])
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def cleanup_preview(db, user: User, project_ids: list[str], *, page: int = 1, page_size: int = 50) -> dict:
    projects = []
    candidates = []
    total = 0
    cutoff = time.time_ns() - EXPORT_RETENTION_SECONDS * 1_000_000_000
    for project_id in list(dict.fromkeys(project_ids)):
        project = owned_project(db, user, project_id)
        snapshot = current_snapshot(db, user.id, project_id)
        blocked = db.scalar(select(Task.id).where(Task.project_id == project_id, Task.owner_id == user.id, Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1)) is not None
        if snapshot is None:
            projects.append({"project_id": project_id, "name": project.name, "count": 0, "size_bytes": 0, "blocked": blocked, "complete": False, "snapshot_id": None, "directories": []})
            continue
        index, summary = snapshot
        with index_connection(index) as connection:
            where = "kind='file' AND module IN ('00_temp','.cache','cache') AND mtime_ns<?"
            rows = connection.execute(f"SELECT module,count(*) AS count,sum(size_bytes) AS size_bytes FROM entries WHERE {where} GROUP BY module", (cutoff,)).fetchall()
            count = sum(row["count"] for row in rows)
            size_bytes = sum(row["size_bytes"] for row in rows)
            total += count
            entries = connection.execute(f"SELECT id,relative_path,size_bytes FROM entries WHERE {where} ORDER BY sort_key LIMIT ?", (cutoff, page * page_size)).fetchall()
        projects.append({"project_id": project_id, "name": project.name, "count": count, "size_bytes": size_bytes, "blocked": blocked, "complete": summary["complete"], "snapshot_id": summary["snapshot_id"], "directories": [dict(row) for row in rows]})
        candidates.extend({"project_id": project_id, "project_name": project.name, **dict(entry)} for entry in entries)
    candidates.sort(key=lambda item: (item["project_id"], item["relative_path"]))
    return {"projects": projects, "items": candidates[(page - 1) * page_size:page * page_size], "total": total, "older_than_days": 7}


def _execute_resource_cleanup(claim: TaskClaim) -> TaskOutcome:
    context = EngineExecutionContext(claim)
    results = []
    references = claim.payload["snapshots"]
    for index, reference in enumerate(references):
        context.check()
        project_id = reference["project_id"]
        result = {"project_id": project_id, "deleted_count": 0, "deleted_bytes": 0, "skipped_count": 0, "failed_count": 0, "blocked": False}
        with SessionLocal() as db:
            user = db.get(User, claim.owner_id)
            if user is None:
                raise ResourceError("任务所属用户不存在", 404)
            # Submission locks this same Project row. Keep the lock throughout
            # deletion so no new production task can race the idle check.
            project = owned_project(db, user, project_id, lock=True)
            result["name"] = project.name
            busy = db.scalar(select(Task.id).where(
                Task.project_id == project_id, Task.owner_id == user.id,
                Task.id != claim.task_id, Task.status.in_(ACTIVE_TASK_STATUSES),
            ).limit(1))
            if busy:
                result["blocked"] = True
                results.append(result)
                continue
            root = project_root(db, user, project_id)
            snapshot_path = internal_path(db, user.id, project_id, f"{reference['snapshot_id']}.sqlite")
            cutoff = time.time_ns() - EXPORT_RETENTION_SECONDS * 1_000_000_000
            with index_connection(snapshot_path) as connection:
                entries = connection.execute("SELECT * FROM entries WHERE kind='file' AND module IN ('00_temp','.cache','cache') AND mtime_ns<?", (cutoff,))
                for record in entries:
                    context.check()
                    try:
                        if record["relative_path"].split("/", 1)[0] not in CACHE_MODULES:
                            result["skipped_count"] += 1
                            continue
                        path = safe_regular_path(root, record["relative_path"])
                        expected = (record["size_bytes"], record["mtime_ns"], record["ctime_ns"], int(record["device"]), int(record["inode"]))
                        current = path.stat()
                        if current.st_mtime_ns >= cutoff or file_identity(current) != expected:
                            result["skipped_count"] += 1
                            continue
                        path.unlink()
                        result["deleted_count"] += 1
                        result["deleted_bytes"] += current.st_size
                    except (FileNotFoundError, ValueError):
                        result["skipped_count"] += 1
                    except OSError:
                        result["failed_count"] += 1
            db.commit()
        results.append(result)
        update_progress(claim, int((index + 1) * 95 / len(references)), f"已检查 {index + 1}/{len(references)} 个项目")
    return _outcome(claim, {"projects": results, "older_than_days": 7})
