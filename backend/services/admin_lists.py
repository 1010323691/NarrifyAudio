"""Paged administrative event reads, excluding task logs/results/payloads."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import case, func, literal, select, union_all
from ..platform.models import AuditLog, Task, utcnow
from ..platform.task_registry import TASK_TYPES
from .task_operations import task_worker_group
from .list_paging import page_meta


def event_union(hours, recent_errors):
    """Union of persisted task failures, audit events and in-process API 5xx.

    Columns: id, time, level, module, type, message. Shared by the paged list,
    the CSV export and the distribution charts so they always agree.
    """
    cutoff = utcnow() - timedelta(hours=max(1, min(hours, 24*30)))
    task_module = case(*[(Task.task_type == t, task_worker_group(t)) for t in TASK_TYPES], else_="worker")
    parts = [select(Task.id.label("id"), Task.updated_at.label("time"), literal("error").label("level"),
                    task_module.label("module"), func.coalesce(Task.error_code, Task.status).label("type"),
                    func.coalesce(Task.error_message, Task.status).label("message"))
             .where(Task.status.in_(["failed", "timeout"]), Task.updated_at >= cutoff),
             select(AuditLog.id, AuditLog.created_at, literal("info"), literal("system"), AuditLog.action,
                    AuditLog.target_type + literal(" ") + func.coalesce(AuditLog.target_id, ""))
             .where(AuditLog.created_at >= cutoff)]
    for item in recent_errors:
        time = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
        if time < cutoff.replace(tzinfo=timezone.utc): continue
        parts.append(select(literal(f"api-{item['time']}-{item['route']}"), literal(time), literal("error"), literal("api"),
                            literal(f"HTTP {item['status']}"), literal(f"{item['method']} {item['route']} 返回 {item['status']}")))
    return union_all(*parts).subquery()


def filtered_events(events, level, module, search):
    statement = select(events)
    if level != "all": statement = statement.where(events.c.level == level)
    if module != "all": statement = statement.where(events.c.module == module)
    if search.strip():
        statement = statement.where((events.c.type + literal(" ") + events.c.message + literal(" ") + events.c.id).ilike("%" + search.strip() + "%"))
    return statement


def event_page(db, page, size, level, module, search, hours, recent_errors):
    events = event_union(hours, recent_errors)
    statement = filtered_events(events, level, module, search)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.execute(statement.order_by(events.c.time.desc(), events.c.id.desc()).offset((page-1)*size).limit(size)).mappings().all()
    return {"items": [{**r, "time": r["time"].isoformat()} for r in rows], "pagination": page_meta(total, page, size)}


def resource_page(db, root, disk, music_dir, page, size):
    """Catalog aggregates; an actual directory inventory is an explicit action."""
    from ..platform.models import Project, ProjectFile, User
    files = db.execute(select(ProjectFile.kind, func.count(), func.coalesce(func.sum(ProjectFile.size_bytes), 0)).join(Project, Project.id == ProjectFile.project_id).where(Project.deleted_at.is_(None), ProjectFile.deleted_at.is_(None)).group_by(ProjectFile.kind)).all()
    project_counts = select(Project.owner_id, func.count().label("count")).where(Project.deleted_at.is_(None)).group_by(Project.owner_id).subquery()
    usage = select(ProjectFile.owner_id, func.count().label("count"), func.sum(ProjectFile.size_bytes).label("size")).join(Project, Project.id == ProjectFile.project_id).where(ProjectFile.deleted_at.is_(None), Project.deleted_at.is_(None)).group_by(ProjectFile.owner_id).subquery()
    statement = select(User.username, func.coalesce(project_counts.c.count, 0), func.coalesce(usage.c.count, 0), func.coalesce(usage.c.size, 0)).outerjoin(project_counts, project_counts.c.owner_id == User.id).outerjoin(usage, usage.c.owner_id == User.id).order_by(func.coalesce(usage.c.size, 0).desc(), User.id)
    total = db.scalar(select(func.count()).select_from(User)) or 0
    users = [{"username": username, "project_count": projects, "file_count": count, "size_bytes": size, "registered_file_count": count, "registered_file_bytes": size} for username, projects, count, size in db.execute(statement.offset((page-1)*size).limit(size)).all()]
    # Index metadata supplies the count without probing each audio file.
    from ..engines import music
    return {"light": True, "root_path": str(root), "disk_total_bytes": disk.total, "disk_used_bytes": disk.used, "disk_free_bytes": disk.free,
            "projects": db.scalar(select(func.count()).select_from(Project).where(Project.deleted_at.is_(None))) or 0,
            "files": [{"kind": kind, "count": count, "size_bytes": size} for kind, count, size in files],
            "users": users, "pagination": page_meta(total, page, size),
            "project_storage": {"file_count": sum(r[1] for r in files), "size_bytes": sum(r[2] for r in files), "categories": [{"kind": kind, "label": kind, "count": count, "size_bytes": size} for kind, count, size in files]},
            "music_library": {"count": len(music.load_index().get("tracks", {})), "size_bytes": None},
            "scope": "首屏统计已登记文件；未登记文件、实际音乐库大小和临时清理候选在请求完整盘点时采集。"}
