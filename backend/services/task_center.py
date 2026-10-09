"""Bounded task-center read models: SQL aggregates first, task details on demand."""
from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..platform.models import Project, Task, TaskEvent
from ..platform.task_categories import TASK_CATEGORIES
from ..platform.task_identity import current_entry_ids
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from .task_views import epoch, legacy_status, task_display_label

GROUP_PAGE_SIZE = 10
ITEM_PAGE_SIZE = 10
PAUSABLE = ACTIVE_TASK_STATUSES - {"paused", "cancelling"}


def _category(category: str) -> tuple[str, ...]:
    if category not in TASK_CATEGORIES:
        raise ValueError("无效的任务分区")
    return TASK_CATEGORIES[category]


def _visible(user_id: str):
    return (
        Task.owner_id == user_id,
        Project.owner_id == user_id,
        Project.deleted_at.is_(None),
        Task.status.not_in(("failed", "cancelled")),
    )


def _base(user_id: str, category: str | None = None, project_id: str | None = None):
    category_expr = case(*[(Task.task_type.in_(types), name) for name, types in TASK_CATEGORIES.items()])
    types = _category(category) if category is not None else tuple(name for names in TASK_CATEGORIES.values() for name in names)
    stmt = select(
        Task.id, Task.project_id, Project.name.label("project_name"),
        Task.status, Task.created_at, category_expr.label("category"),
    ).join(Project, Project.id == Task.project_id).where(*_visible(user_id), Task.id.in_(current_entry_ids(user_id, types, project_id)))
    if category is not None:
        stmt = stmt.where(Task.task_type.in_(_category(category)))
    else:
        stmt = stmt.where(category_expr.is_not(None))
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    return stmt.cte("visible_tasks")


def _count_status(column, statuses):
    return func.sum(case((column.in_(statuses), 1), else_=0))


def _counts(base):
    return [
        func.count().label("task_count"),
        _count_status(base.c.status, {"succeeded"}).label("succeeded_count"),
        _count_status(base.c.status, ACTIVE_TASK_STATUSES).label("active_count"),
        _count_status(base.c.status, PAUSABLE).label("pausable_count"),
        _count_status(base.c.status, {"paused"}).label("resumable_count"),
    ]


def summary(db: Session, user_id: str) -> dict:
    base = _base(user_id)
    rows = db.execute(select(
        base.c.category, func.count().label("task_count"),
        func.count(func.distinct(base.c.project_id)).label("project_count"),
        _count_status(base.c.status, ACTIVE_TASK_STATUSES).label("active_count"),
    ).group_by(base.c.category)).mappings().all()
    by_category = {row["category"]: dict(row) for row in rows}
    return {"items": [by_category.get(name, {
        "category": name, "task_count": 0, "project_count": 0, "active_count": 0,
    }) for name in TASK_CATEGORIES]}


def groups(db: Session, user_id: str, category: str, page: int = 1, page_size: int = GROUP_PAGE_SIZE) -> dict:
    base = _base(user_id, category)
    grouped = select(
        base.c.project_id, base.c.project_name, *_counts(base),
        func.max(base.c.created_at).label("latest_at"),
    ).group_by(base.c.project_id, base.c.project_name).subquery()
    total = db.scalar(select(func.count()).select_from(grouped)) or 0
    # Select a page BEFORE finding its most recent status. This is a single SQL
    # statement, never an ORM relationship query per project.
    paged = select(grouped).order_by(
        case((grouped.c.active_count > 0, 0), else_=1),
        grouped.c.latest_at.desc(), grouped.c.project_id,
    ).offset((page - 1) * page_size).limit(page_size).cte("project_page")
    latest_status = select(base.c.status).where(base.c.project_id == paged.c.project_id).order_by(
        base.c.created_at.desc(), base.c.id.desc(),
    ).limit(1).scalar_subquery()
    rows = db.execute(select(paged, latest_status.label("latest_status")).order_by(
        case((paged.c.active_count > 0, 0), else_=1), paged.c.latest_at.desc(), paged.c.project_id,
    )).mappings().all()
    result = []
    for row in rows:
        item = dict(row)
        item["latest"] = epoch(item.pop("latest_at"))
        item["latest_status"] = legacy_status(item["latest_status"])
        result.append(item)
    return {"items": result, "total": total, "page": page, "page_size": page_size}


def items(db: Session, user_id: str, category: str, project_id: str, filter: str = "all", page: int = 1,
          page_size: int = ITEM_PAGE_SIZE) -> dict:
    if filter not in {"all", "active", "completed"}:
        raise ValueError("无效的任务状态筛选")
    base = _base(user_id, category, project_id)
    counts = {key: int(value or 0) for key, value in db.execute(select(*_counts(base))).mappings().one().items()}
    filtered = select(base.c.id)
    if filter == "active":
        filtered = filtered.where(base.c.status.in_(ACTIVE_TASK_STATUSES))
        total = counts["active_count"]
    elif filter == "completed":
        filtered = filtered.where(base.c.status.in_(("succeeded", "timeout")))
        total = counts["task_count"] - counts["active_count"]
    else:
        total = counts["task_count"]
    # Project names and labels are scalar columns; do not hydrate Task payloads
    # (which include full configuration/input snapshots) just to display a label.
    rows = db.execute(select(
        Task.id, Task.project_id, Project.name.label("project_name"), Task.task_type,
        Task.status, Task.progress, Task.error_message, Task.error_code, Task.created_at,
        Task.payload["label"].as_string().label("label"),
        Task.payload["source_name"].as_string().label("source_name"),
        Task.payload["output_name"].as_string().label("output_name"),
    ).join(Project, Project.id == Task.project_id).where(Task.id.in_(filtered)).order_by(
        Task.created_at.desc(), Task.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size)).mappings().all()
    progress = {}
    if rows:
        # A correlated indexed top-one lookup for each of one page of tasks,
        # all in one statement; old progress/log payloads never leave SQL.
        latest_progress = select(TaskEvent.payload).where(
            TaskEvent.task_id == Task.id, TaskEvent.event_type == "progress",
        ).order_by(TaskEvent.sequence.desc()).limit(1).scalar_subquery()
        progress = dict(db.execute(select(Task.id, latest_progress).where(
            Task.id.in_([row["id"] for row in rows]),
        )).all())

    result = []
    for row in rows:
        label = task_display_label(row["task_type"], {
            "label": row["label"], "source_name": row["source_name"], "output_name": row["output_name"],
        })
        payload = progress.get(row["id"])
        result.append({
            "id": row["id"], "project_id": row["project_id"], "project_name": row["project_name"],
            "task_type": row["task_type"], "label": str(label), "status": legacy_status(row["status"]),
            "progress": max(0.0, min(1.0, row["progress"] / 100.0)),
            "current": str(payload.get("current") or "") if isinstance(payload, dict) else "",
            "error": row["error_message"] or "", "error_code": row["error_code"] or "",
            "created": epoch(row["created_at"]), "created_at": row["created_at"].isoformat(),
        })
    return {"items": result, "total": total, "counts": counts, "page": page, "page_size": page_size}
