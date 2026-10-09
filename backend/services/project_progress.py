"""Fast persisted reads; expensive reconciliation belongs to the durable Worker."""
from datetime import timedelta
from uuid import uuid4
from pathlib import Path
from typing import Callable

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..platform.database import SessionLocal
from ..platform.models import OutboxEvent, Project, ProjectProgress, ProjectProgressRefresh, Task, User, utcnow
from ..platform.progress_refresh import artifact_signature, artifacts_busy, earliest_start
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..platform.storage import lock_storage_migration, safe_project_workspace_path, storage_migration
from ..platform.task_submission import submit_task_record
from ..core.file_lock import exclusive_file_lock
from ..core.safe_filesystem import is_link_or_junction, safe_regular_path
from .task_operations import owned_project

STAGE_SECTIONS = {
    'text': ('02_split_text',),
    'catalog': ('03_parsed_json', '04_voice_profiles'),
    'production': ('05_audio_chunk', '06_audio_merge', '08_bgm'),
}


def _progress_request_lock(root: Path) -> Path:
    """Reject linked temporary directories and locks before opening a file."""
    temporary = root / '00_temp'
    lock = temporary / 'progress-request.lock'
    if any(is_link_or_junction(path) for path in (root, temporary, lock)):
        raise ValueError('进度锁路径不可安全访问')
    temporary.mkdir(parents=True, exist_ok=True)
    temporary = safe_regular_path(root, '00_temp', directory=True)
    if lock.exists():
        return safe_regular_path(root, '00_temp/progress-request.lock')
    if not lock.resolve().is_relative_to(temporary):
        raise ValueError('进度锁路径越界')
    return lock


def progress_signature(db: Session, user: User, project_id: str, root: Path) -> str:
    return artifact_signature(db, user, project_id, root)


def request_progress_refresh(user_id: str, project_id: str) -> None:
    """Submit after the response, using fresh ownership and session state."""
    with SessionLocal() as db:
        # The HTTP migration guard has ended by now. Resolve and use the root
        # only while this fresh transaction holds its own shared guard.
        if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
            return
        user = db.get(User, user_id)
        if user is None or owned_project(db, user_id, project_id) is None:
            return
        root = safe_project_workspace_path(db, user.username, project_id)
        if root is None:
            return
        signature = progress_signature(db, user, project_id, root)
        snapshot = db.get(ProjectProgress, project_id)
        if snapshot is None or snapshot.signature != signature or artifacts_busy(root):
            _submit_progress_refresh(db, user, project_id, root, signature)
        else:
            state = db.get(ProjectProgressRefresh, project_id)
            active = db.get(Task, state.task_id) if state is not None and state.task_id else None
            if state is not None and state.dirty and (active is None or active.status not in ACTIVE_TASK_STATUSES):
                state.dirty = False
                state.task_id = None
                db.commit()


def _submit_progress_refresh(db: Session, user: User, project_id: str, root: Path, signature: str) -> None:
    # Independent card reads coalesce to one task under the submission lock.
    try:
        lock = _progress_request_lock(root)
    except (OSError, ValueError):
        lock = None
    try:
        if lock is not None:
            with exclusive_file_lock(lock, timeout=1):
                project = db.scalar(select(Project).where(
                    Project.id == project_id, Project.owner_id == user.id,
                    Project.deleted_at.is_(None)).with_for_update())
                if project is None:
                    return
                state = db.scalar(select(ProjectProgressRefresh).where(
                    ProjectProgressRefresh.project_id == project_id).with_for_update())
                if state is None:
                    state = ProjectProgressRefresh(project_id=project_id)
                    db.add(state)
                state.requested_signature = signature
                state.dirty = True
                active = db.scalar(select(Task).where(
                    Task.project_id == project_id, Task.task_type == 'project.progress',
                    Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1))
                if active is not None:
                    state.task_id = active.id
                    db.commit()
                    return
                now = utcnow()
                due = earliest_start(state, now)
                task = submit_task_record(db, user, project_id=project_id, task_type='project.progress',
                    payload={'signature': signature}, idempotency_key=f'progress:{project_id}:{uuid4().hex}', commit=False)
                if due > now:
                    task.status = 'retrying'
                    task.next_attempt_at = due
                state.task_id = task.id
                state.next_due_at = due
                db.flush()
                db.execute(update(OutboxEvent).where(
                    OutboxEvent.aggregate_type == 'task', OutboxEvent.aggregate_id == task.id,
                    OutboxEvent.published_at.is_(None)).values(available_at=due))
                db.commit()
    except (TimeoutError, OSError):
        pass  # Keep serving the snapshot; a later read can retry reconciliation.


def progress_summary(db: Session, user: User, project_id: str, root: Path, section: str | None,
                     *, schedule_refresh: Callable[[], None] | None = None) -> dict:
    snapshot = db.get(ProjectProgress, project_id)
    signature = progress_signature(db, user, project_id, root)
    if snapshot is None or snapshot.signature != signature or artifacts_busy(root):
        if schedule_refresh is not None:
            schedule_refresh()
        else:
            _submit_progress_refresh(db, user, project_id, root, signature)
    if snapshot is None:
        return {}  # Existing views show "正在读取进度" until the first snapshot arrives.
    stages = snapshot.stages
    keys = STAGE_SECTIONS[section] if section else sum(STAGE_SECTIONS.values(), ())
    return {key: stages[key] for key in keys if key in stages}


def refresh_due_progress(limit=100):
    """Trailing refresh survives closing the page; bounded coordinator callback."""
    if not 1 <= limit <= 100:
        raise ValueError('progress refresh page must contain 1..100 projects')
    with SessionLocal() as db:
        candidates = db.execute(select(ProjectProgressRefresh.project_id, Project.owner_id)
            .join(Project, Project.id == ProjectProgressRefresh.project_id)
            .outerjoin(Task, Task.id == ProjectProgressRefresh.task_id)
            .where(Project.deleted_at.is_(None), ProjectProgressRefresh.dirty.is_(True),
                   ProjectProgressRefresh.next_due_at <= utcnow(),
                   or_(Task.id.is_(None), Task.status.not_in(ACTIVE_TASK_STATUSES)))
            .order_by(ProjectProgressRefresh.next_due_at, ProjectProgressRefresh.project_id).limit(limit)).all()
    for project_id, owner_id in candidates:
        request_progress_refresh(owner_id, project_id)
        # Unsafe/temporarily inaccessible projects must not starve later pages.
        with SessionLocal.begin() as db:
            state = db.get(ProjectProgressRefresh, project_id)
            active = db.get(Task, state.task_id) if state is not None and state.task_id else None
            if state is not None and state.dirty and (active is None or active.status not in ACTIVE_TASK_STATUSES):
                state.task_id = None
                state.next_due_at = utcnow() + timedelta(seconds=300)
    return len(candidates)
