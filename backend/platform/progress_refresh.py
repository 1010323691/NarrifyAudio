"""Artifact signatures and fenced publication of durable progress refresh state."""
import hashlib
import json
import time
from datetime import timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from ..core.workspace_epochs import versions
from .models import ProjectProgress, ProjectProgressRefresh, utcnow

MIN_INTERVAL = 30


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def artifacts_busy(root):
    try:
        return versions(root)[1]
    except (OSError, ValueError):
        return True


def artifact_signature(db, user, project_id, root):
    # Constant-size probes catch additions/removals even in old workspaces;
    # managed epochs cover deep replacements. No pure progress/heartbeat input.
    paths = [Path(root) / name for name in (
        '01_input', '02_split_text', '03_parsed_json', '05_audio_chunk',
        '06_audio_merge', '07_output', '08_bgm', '04_voice_profiles/voice_config.json')]
    fingerprints = []
    for path in paths:
        try:
            stat = path.stat()
            fingerprints.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            fingerprints.append(None)
    try:
        epoch = versions(root)
    except (OSError, ValueError):
        epoch = 'unavailable'
    value = [str(root), fingerprints, epoch, int(time.time() // 300)]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def earliest_start(state, now):
    return max(now, as_utc(state.last_started_at) + timedelta(seconds=MIN_INTERVAL)) if state.last_started_at else now


def execution_started(db, claim, user, root):
    state = db.scalar(select(ProjectProgressRefresh).where(
        ProjectProgressRefresh.project_id == claim.project_id).with_for_update())
    if state is None:
        state = ProjectProgressRefresh(project_id=claim.project_id)
        db.add(state)
    signature = artifact_signature(db, user, claim.project_id, root)
    state.task_id = claim.task_id
    state.requested_signature = signature
    state.dirty = True
    state.last_started_at = utcnow()
    state.next_due_at = earliest_start(state, state.last_started_at)
    return signature


def publish_snapshot(db, task, metadata, user, root):
    snapshot = db.get(ProjectProgress, task.project_id)
    if snapshot is None:
        snapshot = ProjectProgress(project_id=task.project_id)
        db.add(snapshot)
    snapshot.signature = metadata['signature']
    snapshot.stages = metadata['stages']
    snapshot.updated_at = utcnow()
    state = db.scalar(select(ProjectProgressRefresh).where(
        ProjectProgressRefresh.project_id == task.project_id).with_for_update())
    if state is not None:
        signature = artifact_signature(db, user, task.project_id, root)
        state.requested_signature = signature
        state.dirty = signature != snapshot.signature or artifacts_busy(root)
        state.task_id = None
        state.next_due_at = earliest_start(state, utcnow())
