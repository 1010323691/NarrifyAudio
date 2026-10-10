"""Path-local delivery authority, written with TaskResult and backfilled by Worker."""
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..core.object_keys import object_key_lookup_key
from ..core.safe_filesystem import file_identity, identity_matches, safe_regular_path
from .models import CurrentDelivery, DeliveryIndexState, Project, ProjectFile, Task, TaskResult
from .storage import safe_project_workspace_path
from .resource_delivery import ARCHIVE_TYPES, DELIVERY_TYPES, _allowed, _relative


def _stamp(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value or datetime.min.replace(tzinfo=timezone.utc)


def _path_reference(root, value):
    """Revocations must survive disappearance; never accept traversal or links."""
    if not isinstance(value, str) or not value:
        return None
    try:
        path = Path(value)
        relative = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
        if any(part in ("", ".", "..") for part in relative.split("/")) or "\\" in relative or ":" in relative:
            return None
        (root / relative).resolve().relative_to(root.resolve())
        if (root / relative).exists():
            safe_regular_path(root, relative)
        return relative
    except (OSError, ValueError, RuntimeError):
        return None


def authorities(db, root, task, result):
    values = [result, *[item for item in result.get("files", []) if isinstance(item, dict)]]
    if result.get("complete") is False:
        return [{"relative_path": relative, "identity": None, "valid": False}
                for item in values if (relative := _path_reference(root, item.get("path")))
                and _allowed(task.task_type, relative, result)]
    if "deliveries" in result:
        candidates = result.get("deliveries") or []
    else:
        candidates = []
        if task.task_type in ARCHIVE_TYPES:
            return candidates
        for item in values:
            path = item.get("path")
            catalog = db.get(ProjectFile, item["file_id"]) if item.get("file_id") else None
            if catalog and (catalog.owner_id != task.owner_id or catalog.project_id != task.project_id or catalog.deleted_at):
                catalog = None
            if not path and catalog:
                prefix = db.get(Project, task.project_id).directory_key + "/"
                path = catalog.object_key.removeprefix(prefix) if catalog.object_key.startswith(prefix) else None
            relative = _relative(root, str(path)) if path else None
            if not relative or not _allowed(task.task_type, relative, result):
                continue
            stat = safe_regular_path(root, relative).stat()
            expected = item.get("size") if item.get("path") else catalog.size_bytes if catalog else None
            if expected is None and catalog and Path(catalog.object_key).suffix.lower() == Path(relative).suffix.lower():
                expected = catalog.size_bytes
            if expected is None and item.get("file") == Path(relative).name:
                expected = stat.st_size
            if expected is None or stat.st_size != expected or not task.finished_at or max(stat.st_mtime, stat.st_ctime) > _stamp(task.finished_at).timestamp() + 1:
                continue
            candidates.append({"relative_path": relative, "identity": list(file_identity(stat))})
    rows = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        relative = _path_reference(root, item.get("relative_path"))
        if not relative or not _allowed(task.task_type, relative, result):
            continue
        valid = False
        try:
            stat = safe_regular_path(root, relative).stat()
            valid = bool(stat.st_size and identity_matches(file_identity(stat), item.get("identity")))
        except (OSError, ValueError, RuntimeError):
            pass
        rows.append({"relative_path": relative, "identity": item.get("identity"), "valid": valid})
    return rows


def write_authorities(db, user, task, result, *, locked=False):
    if task.task_type not in DELIVERY_TYPES:
        return
    if not locked:
        db.scalar(select(Project.id).where(Project.id == task.project_id).with_for_update())
    root = safe_project_workspace_path(db, user.username, task.project_id)
    if root is None:
        return
    incoming = {object_key_lookup_key(item["relative_path"]): item for item in authorities(db, root, task, result)}
    keys = list(incoming)
    existing = {}
    for offset in range(0, len(keys), 100):
        existing.update((row.path_key, row) for row in db.scalars(select(CurrentDelivery).where(
            CurrentDelivery.project_id == task.project_id, CurrentDelivery.path_key.in_(keys[offset:offset + 100]))))
    for key, item in incoming.items():
        row = existing.get(key)
        if row and (_stamp(row.finished_at), row.task_id) > (_stamp(task.finished_at), task.id):
            continue
        if row is None:
            row = CurrentDelivery(project_id=task.project_id, path_key=key)
            db.add(row)
        row.owner_id, row.task_id, row.task_type = task.owner_id, task.id, task.task_type
        row.finished_at = task.finished_at
        row.relative_path, row.identity, row.valid = item["relative_path"], item["identity"], item["valid"]
    # Multiple historical tasks in a backfill chunk may target the same row.
    # Flush to make each next indexed lookup see the preceding authority.
    if incoming:
        db.flush()


def backfill_project(db, user, project_id, *, limit=100):
    """Commit is owned by caller; cursor and authority updates are atomic."""
    if not 1 <= limit <= 100:
        raise ValueError("delivery backfill chunk must be 1..100")
    project = db.scalar(select(Project).where(Project.id == project_id, Project.owner_id == user.id,
                                               Project.deleted_at.is_(None)).with_for_update())
    if project is None or safe_project_workspace_path(db, user.username, project_id) is None:
        return False
    state = db.get(DeliveryIndexState, project_id)
    if state is None:
        state = DeliveryIndexState(project_id=project_id, cursor="", complete=False)
        db.add(state)
    if state.complete:
        return True
    rows = db.execute(select(Task, TaskResult).join(TaskResult, TaskResult.task_id == Task.id).where(
        Task.project_id == project_id, Task.owner_id == user.id, Task.status == "succeeded",
        Task.task_type.in_(DELIVERY_TYPES), Task.id > state.cursor).order_by(Task.id).limit(limit)).all()
    for task, stored in rows:
        write_authorities(db, user, task, stored.result, locked=True)
        state.cursor = task.id
    if len(rows) < limit:
        state.complete = True
    return state.complete


def indexed_records(db, user, project_id, root, relatives=None):
    query = select(CurrentDelivery).join(Task, Task.id == CurrentDelivery.task_id).where(
        CurrentDelivery.owner_id == user.id, CurrentDelivery.project_id == project_id,
        CurrentDelivery.valid.is_(True), Task.status == "succeeded")
    if relatives is not None:
        keys = list({object_key_lookup_key(name) for name in relatives})
        rows = []
        for offset in range(0, len(keys), 100):
            rows.extend(db.scalars(query.where(CurrentDelivery.path_key.in_(keys[offset:offset + 100]))))
    else:
        rows = db.scalars(query)
    records = {}
    for row in rows:
        try:
            stat = safe_regular_path(root, row.relative_path).stat()
            if stat.st_size and identity_matches(file_identity(stat), row.identity):
                records[row.relative_path] = {"relative_path": row.relative_path, "identity": row.identity,
                    "task_id": row.task_id, "label": DELIVERY_TYPES[row.task_type][1],
                    "completed_at": row.finished_at.isoformat() if row.finished_at else None,
                    "version": f"V{row.finished_at.strftime('%Y%m%d-%H%M%S')}" if row.finished_at else None}
        except (OSError, ValueError, RuntimeError):
            continue
    return records


_UNRESOLVABLE_RETRY_SECONDS = 600
_unresolvable_until: dict[str, float] = {}


def backfill_pending_project(db):
    """One bounded chunk, fairly rotate unsafe/busy project candidates.

    Returns True only when a resolvable project was worked on. Projects whose
    workspace cannot be resolved (symlink/junction, bad key) are permanent
    conditions: they never count as pending and are remembered in-process for
    a retry window, so neither the busy cadence nor the database is touched
    on every poll on their account.
    """
    import time
    from sqlalchemy import or_
    from .models import User, utcnow
    now = time.monotonic()
    for key in [key for key, until in _unresolvable_until.items() if until <= now]:
        del _unresolvable_until[key]
    candidates = select(Project).outerjoin(DeliveryIndexState,
        DeliveryIndexState.project_id == Project.id).where(Project.deleted_at.is_(None),
        or_(DeliveryIndexState.project_id.is_(None), DeliveryIndexState.complete.is_(False))
        ).order_by(DeliveryIndexState.updated_at.asc().nulls_first(), Project.id)
    skipped: list[str] = list(_unresolvable_until)
    for _ in range(16):
        stmt = candidates.with_for_update(of=Project, skip_locked=True).limit(1)
        if skipped:
            stmt = candidates.where(Project.id.not_in(skipped)).with_for_update(of=Project, skip_locked=True).limit(1)
        project = db.scalar(stmt)
        if project is None:
            return False
        user = db.get(User, project.owner_id)
        if user is None or safe_project_workspace_path(db, user.username, project.id) is None:
            _unresolvable_until[project.id] = now + _UNRESOLVABLE_RETRY_SECONDS
            skipped.append(project.id)
            continue
        backfill_project(db, user, project.id)
        db.flush()
        state = db.get(DeliveryIndexState, project.id)
        if state is None:
            state = DeliveryIndexState(project_id=project.id, cursor="", complete=False)
            db.add(state)
        state.updated_at = utcnow()
        return True
    return False
