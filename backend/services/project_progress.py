"""Fast persisted reads; expensive reconciliation belongs to the durable Worker."""
import hashlib
import json
import time
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..platform.models import ProjectProgress, Task, User
from ..platform.task_submission import submit_task_record
from ..core.file_lock import exclusive_file_lock
from ..core.safe_filesystem import is_link_or_junction, safe_regular_path

STAGE_SECTIONS = {
    'text': ('02_split_text',),
    'catalog': ('03_parsed_json', '04_voice_profiles'),
    'production': ('05_audio_chunk', '06_audio_merge', '08_bgm', '07_output'),
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
    # Constant-size metadata reads, never enumerate audio or parse scripts here.
    paths = [root / name for name in (
        '01_input', '02_split_text', '03_parsed_json', '05_audio_chunk',
        '06_audio_merge', '08_bgm', '04_voice_profiles/voice_config.json',
    )]
    fingerprints = []
    for path in paths:
        try:
            stat = path.stat()
            fingerprints.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            fingerprints.append(None)
    updated = db.scalar(select(func.max(Task.updated_at)).where(
        Task.owner_id == user.id, Task.project_id == project_id,
        Task.task_type != 'project.progress',
    ))
    # Also reconcile external edits periodically; file contents are never read by HTTP.
    value = [str(root), fingerprints, str(updated), int(time.time() // 300)]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def progress_summary(db: Session, user: User, project_id: str, root: Path, section: str | None) -> dict:
    snapshot = db.get(ProjectProgress, project_id)
    signature = progress_signature(db, user, project_id, root)
    if snapshot is None or snapshot.signature != signature:
        # Three independent card reads coalesce to one task under the submission lock.
        try:
            lock = _progress_request_lock(root)
        except (OSError, ValueError):
            # Keep the last snapshot when the lock path is unsafe or unavailable.
            lock = None
        try:
            if lock is not None:
                with exclusive_file_lock(lock, timeout=1):
                    submit_task_record(db, user, project_id=project_id, task_type='project.progress',
                                       payload={'signature': signature},
                                       idempotency_key=f'progress:{project_id}:{signature}:{int(time.time() // 30)}')
                    db.commit()  # Release submission row locks even when an active job was reused.
        except TimeoutError:
            pass  # Another reader is already submitting; the next poll can retry.
        except OSError:
            # An unsafe/unavailable temp directory must never redirect this GET's writes.
            # Keep serving the last snapshot; reconciliation can retry once repaired.
            pass
    if snapshot is None:
        return {}  # Existing views show "正在读取进度" until the first snapshot arrives.
    stages = snapshot.stages
    keys = STAGE_SECTIONS[section] if section else sum(STAGE_SECTIONS.values(), ())
    return {key: stages[key] for key in keys if key in stages}
