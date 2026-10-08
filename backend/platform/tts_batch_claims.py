"""Collection operations for members of an already fenced TTS batch."""
from __future__ import annotations
import secrets
from datetime import timedelta
from sqlalchemy import func, insert, select, update
from .database import SessionLocal
from .models import Task, TaskAttempt, TaskEvent, new_id, utcnow
from .platform_settings import settings
from .storage import lock_storage_migration, storage_migration
from .task_contracts import TaskClaim
from .gpu_scheduler.store import guarded_claim
from ..core.observability import timed_tts_stage


@guarded_claim
@timed_tts_stage('member_claim')
def claim_tts_members(primary, task_ids):
    if not task_ids:
        return []
    # Also bound imported legacy batches that predate the public 500-chapter cap.
    task_ids = task_ids[:499]
    now = utcnow()
    with SessionLocal() as db:
        if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
            return []
        rows = db.scalars(select(Task).where(Task.id.in_(task_ids), Task.owner_id == primary.owner_id,
                                            Task.project_id == primary.project_id, Task.task_type == 'tts.batch',
                                            Task.status.in_(['pending', 'queued', 'retrying']))
                          .order_by(Task.id).with_for_update()).all()
        # Parked/expired attempts use the existing recovery path, not a bulk shortcut.
        from .task_lifecycle import hydrate_task_ui_states
        hydrate_task_ui_states(db, rows)
        numbers = dict(db.execute(select(TaskAttempt.task_id, func.max(TaskAttempt.attempt_no)).where(
            TaskAttempt.task_id.in_(task_ids)).group_by(TaskAttempt.task_id)).all())
        active = set(db.scalars(select(TaskAttempt.task_id).where(TaskAttempt.task_id.in_(task_ids),
                                                                  TaskAttempt.status == 'running')))
        claims, attempts, events = [], [], []
        for row in rows:
            if row.id in active or (row.next_attempt_at and _future(row.next_attempt_at, now)):
                continue
            attempt_no = numbers.get(row.id, 0) + 1
            if attempt_no > settings.task_max_attempts and row.error_code != 'llm_unavailable':
                continue
            if row.payload.get('execution_batch') != primary.payload.get('execution_batch'):
                continue
            attempt_id, token = new_id(), secrets.token_urlsafe(32)
            attempts.append(dict(id=attempt_id, task_id=row.id, attempt_no=attempt_no, worker_id=primary.worker_id,
                                 lease_token=token, status='running', lease_expires_at=now + timedelta(seconds=settings.task_lease_seconds)))
            row.status = 'running'; row.next_attempt_at = None; row.started_at = row.started_at or now
            row.ui_state = {**(row.ui_state or {}), 'tts_slot': primary.attempt_id, 'tts_parked': False}
            row.updated_at = now; row.error_code = ''; row.error_message = ''
            row.event_sequence = (row.event_sequence or 0) + 1
            events.append(dict(id=new_id(), task_id=row.id, sequence=row.event_sequence, event_type='attempt_started',
                               payload={'attempt_id': attempt_id, 'attempt_no': attempt_no, 'worker_id': primary.worker_id}, created_at=now))
            claims.append(TaskClaim(row.id, attempt_id, attempt_no, token, primary.worker_id,
                                    row.owner_id, row.project_id, row.task_type, dict(row.payload)))
        for offset in range(0, len(attempts), 100):
            db.execute(insert(TaskAttempt), attempts[offset:offset + 100])
            db.execute(insert(TaskEvent), events[offset:offset + 100])
        db.commit()
        order = {task_id: index for index, task_id in enumerate(task_ids)}
        return sorted(claims, key=lambda claim: order[claim.task_id])


def _future(value, now):
    from datetime import timezone
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value) > now


def heartbeat_members(claims):
    now = utcnow()
    with SessionLocal.begin() as db:
        for offset in range(0, len(claims), 100):
            chunk = claims[offset:offset + 100]
            rows = {row.id: row for row in db.scalars(select(TaskAttempt).where(
                TaskAttempt.id.in_([c.attempt_id for c in chunk])).with_for_update())}
            for claim in chunk:
                attempt = rows.get(claim.attempt_id)
                if attempt and attempt.task_id == claim.task_id and attempt.lease_token == claim.lease_token and attempt.status == 'running':
                    attempt.lease_expires_at = now + timedelta(seconds=settings.task_lease_seconds)
