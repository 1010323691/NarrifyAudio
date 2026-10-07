"""Named workspace layout and recoverable filesystem/database relocation.

Database IDs remain stable; directory_key is the sole physical location.
Every move is recorded before execution, and reversed on transaction rollback
(or on the next offline migration after an interrupted process).
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
from pathlib import Path

from sqlalchemy import JSON, event, select
from sqlalchemy.orm import Session

from ..core.paths import WORKSPACE_DIR_NAMES
from ..core.safe_filesystem import is_link_or_junction
from .models import Project, ProjectFile, Task, TaskResult, TaskEvent, TextFormatFlow, SystemConfig, utcnow
from .storage import available_file_name, artifact_module, configured_storage_root, object_path, safe_display_name, sha256_file
from .task_lifecycle import ACTIVE_TASK_STATUSES

PROJECT_DIRECTORIES = (*WORKSPACE_DIR_NAMES, "config", "logs")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class ProjectDirectoryConflict(ValueError):
    """A candidate name is occupied; callers may try a readable suffix."""


def validate_project_name(name: str) -> str:
    name = name.strip()
    if (not name or len(name) > 160 or name in {".", ".."}
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.endswith((".", " ")) or _RESERVED.match(name)):
        raise ValueError("项目名称不能包含路径分隔符、Windows 非法字符、保留名称或末尾句点")
    return name


def named_directory_key(db: Session, username: str, name: str, *, project_id: str | None = None) -> str:
    name = validate_project_name(name)
    key = f"{safe_display_name(username)}/{name}"
    for other in db.scalars(select(Project)):
        if other.id != project_id and other.directory_key.casefold() == key.casefold():
            raise ProjectDirectoryConflict("项目目录名称已被占用（名称不区分大小写）")
    root = configured_storage_root(db)
    target = object_path(key, root)
    current = db.get(Project, project_id) if project_id else None
    old = object_path(current.directory_key, root) if current else None
    if target.parent.is_dir():
        for entry in target.parent.iterdir():
            if entry.name.casefold() == name.casefold() and entry != old:
                raise ProjectDirectoryConflict("项目目录已存在，请选择其他项目名称")
    if is_link_or_junction(target.parent) or is_link_or_junction(target):
        raise ValueError("项目目录不可安全访问")
    return key


def ensure_project_directories(path: Path) -> None:
    for name in PROJECT_DIRECTORIES:
        (path / name).mkdir(parents=True, exist_ok=True)


def _rewrite(value, replacements: dict[str, str], root: Path | None = None):
    if isinstance(value, dict):
        return {key: _rewrite(item, replacements, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, replacements, root) for item in value]
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        # Lookup only complete path prefixes instead of sorting thousands of
        # replacements for every string in a historical task event.
        for match in reversed(list(re.finditer(r"[/\\]", value))):
            prefix = value[:match.start()]
            if prefix in replacements:
                return replacements[prefix] + value[match.start():]
        if root is not None and (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)):
            normalized = value.replace("\\", "/")
            # Older administrator storage-root moves may have left absolute
            # provenance paths pointing to an earlier deployment directory.
            for match in re.finditer("/", normalized):
                suffix = normalized[match.end():]
                rewritten = _rewrite(suffix, replacements)
                if rewritten != suffix:
                    return str(root / rewritten)
    return value


def _undo(actions: list[dict]) -> None:
    for action in reversed(actions):
        if action["op"] == "move":
            source, target = Path(action["source"]), Path(action["target"])
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                target.rename(source)
        elif action["op"] == "write":
            Path(action["path"]).write_bytes(base64.b64decode(action["before"]))
        elif action["op"] == "rmdir":
            Path(action["path"]).mkdir(parents=True, exist_ok=True)


def recover_layout_moves(db: Session) -> None:
    directory = configured_storage_root(db) / ".layout-migrations"
    if not directory.is_dir():
        return
    for journal in directory.glob("*.json"):
        lines = journal.read_text(encoding="utf-8").splitlines()
        details = json.loads(lines[0])
        for index, line in enumerate(lines[1:], 1):
            try:
                details["actions"].append(json.loads(line))
            except ValueError:
                if index != len(lines) - 1:
                    raise
                # An interrupted final append cannot have performed its move.

        project = db.get(Project, details["project_id"])
        # The journal survives a crash between DB commit and file cleanup.
        marker = db.get(SystemConfig, f"layout.move.{details['project_id']}")
        if marker is None or marker.value.get("token") != details["token"]:
            _undo(details["actions"])
        journal.unlink()


def relocate_project(db: Session, project: Project, target_key: str, *, normalize: bool = False) -> None:
    if db.scalar(select(Task.id).where(Task.project_id == project.id, Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1)):
        raise ValueError("项目仍有未完成任务，请完成或取消后再修改项目目录")
    root = configured_storage_root(db)
    old_key = project.directory_key
    source, target = object_path(old_key, root), object_path(target_key, root)
    if is_link_or_junction(source.parent) or is_link_or_junction(source) or is_link_or_junction(target):
        raise ValueError("项目目录不可安全访问")
    if source != target and target.exists():
        raise ValueError("目标项目目录已存在")
    if source.is_dir() and any(is_link_or_junction(path) for path in source.rglob("*")):
        raise ValueError("项目包含链接，请先移除链接后迁移")
    files = list(db.scalars(select(ProjectFile).where(ProjectFile.project_id == project.id)))
    task_rows = list(db.scalars(select(Task).where(Task.project_id == project.id)))
    task_types = {row.id: row.task_type for row in task_rows}
    file_types = {}
    task_ids = list(task_types)
    results = list(db.scalars(select(TaskResult).where(TaskResult.task_id.in_(task_ids)))) if task_ids else []
    for row in results:
        def collect(value):
            if isinstance(value, dict):
                if value.get("file_id"):
                    file_types[value["file_id"]] = task_types[row.task_id]
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(row.result)
    legacy_key = f"{project.owner.username}/{project.id}"
    replacements = {old_key: target_key, str(source): str(target), legacy_key: target_key}
    actions = []
    if source != target and source.exists():
        actions.append({"op": "move", "source": str(source), "target": str(target)})
    reserved: dict[str, set[str]] = {}
    for item in files:
        if not item.object_key.startswith(old_key + "/"):
            continue
        relative = Path(item.object_key[len(old_key) + 1:])
        if ".." in relative.parts or relative.is_absolute():
            raise ValueError("项目文件路径越界")
        new_relative = relative
        legacy = relative.parts[0] not in PROJECT_DIRECTORIES or (
            len(relative.parts) == 3 and relative.parts[0] == "01_input")
        if normalize and legacy:
            module = "01_input" if item.kind == "input" else artifact_module(file_types.get(item.id, ""), item.original_name)
            names = reserved.setdefault(module, set())
            name = available_file_name(source / module, item.original_name, reserved=names)
            names.add(name)
            new_relative = Path(module) / name
        new_key = f"{target_key}/{new_relative.as_posix()}"
        for legacy_relative in (
            Path(item.id) / safe_display_name(item.original_name),
            Path("01_input") / item.id / safe_display_name(item.original_name),
            new_relative,
        ):
            replacements[f"{legacy_key}/{legacy_relative.as_posix()}"] = new_key
        replacements[item.object_key] = new_key
        replacements[str(root / item.object_key)] = str(root / new_key)
        if new_relative != relative and (source / relative).is_file():
            actions.append({"op": "move", "source": str(target / relative), "target": str(target / new_relative)})
        item.object_key = new_key
    journal = root / ".layout-migrations" / f"{project.id}.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    details = {"project_id": project.id, "target_key": target_key, "token": secrets.token_hex(16), "actions": []}
    # Append each action once. Rewriting the entire journal after every move
    # makes large workspaces quadratic and repeatedly copies JSON backups.
    journal.write_text(json.dumps(details, ensure_ascii=False) + "\n", encoding="utf-8")
    def save(action):
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(action, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        details["actions"].append(action)
    def move(old: Path, new: Path):
        save({"op": "move", "source": str(old), "target": str(new)})
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
    finished = False
    def rollback(_session):
        nonlocal finished
        if finished:
            return
        finished = True
        _undo(details["actions"])
        journal.unlink(missing_ok=True)
    def committed(_session):
        nonlocal finished
        finished = True
        journal.unlink(missing_ok=True)
    event.listen(db, "after_rollback", rollback, once=True)
    event.listen(db, "after_commit", committed, once=True)
    for action in actions:
        move(Path(action["source"]), Path(action["target"]))
    ensure_project_directories(target)
    if normalize:
        # Preserve unknown/uncatalogued data too, without leaving arbitrary roots.
        for entry in list(target.iterdir()):
            if entry.name in PROJECT_DIRECTORIES:
                continue
            if is_link_or_junction(entry):
                raise ValueError("旧项目包含不可安全迁移的链接")
            if entry.is_dir() and not any(entry.iterdir()):
                save({"op": "rmdir", "path": str(entry)})
                entry.rmdir()
                continue
            if entry.name in {".tasks", ".cache", "cache"}:
                base = "tasks" if entry.name == ".tasks" else "历史缓存"
                dest = target / "00_temp" / available_file_name(target / "00_temp", base)
            else:
                history = target / "07_output" / "历史文件"
                base = "历史目录" if re.fullmatch(r"[0-9a-fA-F-]{36}", entry.name) else entry.name
                dest = history / available_file_name(history, base)
            replacements[str(source / entry.name)] = str(dest)
            replacements[f"{old_key}/{entry.name}"] = dest.relative_to(root).as_posix()
            move(entry, dest)
        inputs = target / "01_input"
        for entry in list(inputs.iterdir()):
            if entry.is_dir() and not is_link_or_junction(entry) and not any(entry.iterdir()):
                save({"op": "rmdir", "path": str(entry)})
                entry.rmdir()
    project.directory_key = target_key
    project.updated_at = utcnow()
    marker_key = f"layout.move.{project.id}"
    marker = db.get(SystemConfig, marker_key)
    if marker is None:
        marker = SystemConfig(key=marker_key, value={"token": details["token"]})
        db.add(marker)
    else:
        marker.value = {"token": details["token"]}
    rows = [*task_rows, *results]
    if task_ids:
        rows.extend(db.scalars(select(TaskEvent).where(TaskEvent.task_id.in_(task_ids))))
    rows.extend(db.scalars(select(TextFormatFlow).where(TextFormatFlow.project_id == project.id)))
    for row in rows:
        for column in row.__table__.columns:
            if isinstance(column.type, JSON):
                value = getattr(row, column.name)
                rewritten = _rewrite(value, replacements, root)
                if rewritten != value:
                    setattr(row, column.name, rewritten)
    indexed_files = {item.object_key: item for item in files}
    for path in target.rglob("*.json"):
        if is_link_or_junction(path) or any(is_link_or_junction(parent) for parent in path.parents if parent != root):
            continue
        try:
            before = path.read_bytes()
            value = json.loads(before)
        except (ValueError, OSError):
            continue
        rewritten = _rewrite(value, replacements, root)
        if rewritten != value:
            save({"op": "write", "path": str(path), "before": base64.b64encode(before).decode("ascii")})
            path.write_text(json.dumps(rewritten, ensure_ascii=False, indent=2), encoding="utf-8")
            item = indexed_files.get(path.relative_to(root).as_posix())
            if item is not None:
                item.size_bytes = path.stat().st_size
                item.sha256 = sha256_file(path)
