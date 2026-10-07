"""Bounded, project-scoped task status for the production overview."""
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..platform.models import Task
from ..platform.task_identity import current_entry_ids
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..platform.task_registry import SUPPORTED_TASK_TYPES
from .task_operations import owned_project


def overview_tasks(db: Session, user_id: str, project_id: str) -> dict:
    if owned_project(db, user_id, project_id) is None:
        raise ValueError("项目不存在")
    filters = (Task.owner_id == user_id, Task.project_id == project_id,
               Task.id.in_(current_entry_ids(user_id, SUPPORTED_TASK_TYPES, project_id)))
    rows = db.execute(select(Task.task_type, Task.status, func.count().label("count"))
        .where(*filters, Task.status.not_in(("succeeded", "cancelled")))
        .group_by(Task.task_type, Task.status)
        .order_by(case((Task.status.in_(ACTIVE_TASK_STATUSES), 0), else_=1), Task.task_type, Task.status)).mappings().all()
    failures = db.execute(select(Task.id, Task.task_type, func.substr(Task.error_message, 1, 500).label("error_message"))
        .where(*filters, Task.status.in_(("failed", "timeout")))
        .order_by(Task.created_at.desc(), Task.id.desc()).limit(3)).mappings().all()
    return {"statuses": [dict(row) for row in rows], "failures": [dict(row) for row in failures],
            "failure_count": sum(row["count"] for row in rows if row["status"] in {"failed", "timeout"})}
