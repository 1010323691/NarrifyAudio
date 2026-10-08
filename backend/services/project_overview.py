"""Bounded, project-scoped task status for the production overview."""
from pathlib import Path

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ..platform.models import Task, TaskResult
from ..platform.task_identity import current_entry_ids
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..platform.task_registry import SUPPORTED_TASK_TYPES
from ..core.bounded_json import is_script_file, read_json
from ..core.safe_filesystem import is_link_or_junction
from .task_operations import owned_project


def overview_tasks(db: Session, user_id: str, project_id: str, *, root: Path | None = None) -> dict:
    if owned_project(db, user_id, project_id) is None:
        raise ValueError("项目不存在")
    filters = (Task.owner_id == user_id, Task.project_id == project_id,
               Task.id.in_(current_entry_ids(user_id, SUPPORTED_TASK_TYPES, project_id)))
    # Old releases submitted book-analysis objects as chapter synthesis tasks.
    # These have no production entry to retry; retain history but omit them from
    # current project health. Never infer this from a name, progress, or error text.
    reports = []
    if root is not None:
        subjects = db.execute(select(Task.payload['scripts'][0].as_string(),
                                     Task.payload['script'].as_string()).where(
            *filters, Task.task_type == 'tts.batch', Task.status.in_(('failed', 'timeout')),
        ).distinct()).all()
        names = {name for row in subjects for name in row
                 if name and Path(name).name == name and Path(name).suffix == '.json'}
        # Provenance survives manual deletion of the legacy report. Select only
        # the published key, never the potentially large book-analysis result.
        report_names = set()
        if names:
            key = TaskResult.result['object_key'].as_string()
            keys = db.scalars(select(key).join(Task, Task.id == TaskResult.task_id).where(
                Task.owner_id == user_id, Task.project_id == project_id,
                Task.task_type == 'book.analyze', Task.status == 'succeeded',
                or_(*[key.endswith('/03_parsed_json/' + name, autoescape=True) for name in names]),
            ))
            report_names = {Path(key).name for key in keys if key}
        for name in names:
            path = root / '03_parsed_json' / name
            if (Path(name).name != name or path.suffix != '.json'
                    or is_link_or_junction(path.parent) or is_link_or_junction(path)
                    or is_script_file(path)):
                continue
            if not path.exists():
                if name in report_names:
                    reports.append(name)
                continue
            if not path.is_file():
                continue
            try:
                if isinstance(read_json(path), dict):
                    reports.append(path.name)
            except (OSError, ValueError):
                pass  # Missing/corrupt chapter errors remain actionable.
    if reports:
        report_entry = or_(*[
            or_(and_(Task.payload['scripts'][0].as_string() == name,
                     Task.payload['scripts'][1].as_string().is_(None)),
                and_(Task.payload['scripts'].as_string().is_(None),
                     Task.payload['script'].as_string() == name))
            for name in reports
        ])
        filters += (~and_(Task.task_type == 'tts.batch',
                         Task.status.in_(('failed', 'timeout')), func.coalesce(report_entry, False)),)
    rows = db.execute(select(Task.task_type, Task.status, func.count().label("count"))
        .where(*filters, Task.status.not_in(("succeeded", "cancelled")))
        .group_by(Task.task_type, Task.status)
        .order_by(case((Task.status.in_(ACTIVE_TASK_STATUSES), 0), else_=1), Task.task_type, Task.status)).mappings().all()
    failures = db.execute(select(Task.id, Task.task_type, func.substr(Task.error_message, 1, 500).label("error_message"))
        .where(*filters, Task.status.in_(("failed", "timeout")))
        .order_by(Task.created_at.desc(), Task.id.desc()).limit(3)).mappings().all()
    return {"statuses": [dict(row) for row in rows], "failures": [dict(row) for row in failures],
            "failure_count": sum(row["count"] for row in rows if row["status"] in {"failed", "timeout"})}
