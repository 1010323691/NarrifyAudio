"""Aggregations behind the admin console's charts.

Everything here reads persisted tables (tasks, quota ledger, metric samples,
audit log) so the charts survive API restarts; the only process-local series
is the API latency view in ``core.observability``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil
from statistics import mean

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from ..platform.models import QuotaTransaction, SystemMetricSample, Task, User, UserSession, utcnow
from ..platform.task_registry import WORKER_GROUP_NAMES, task_worker_group
from .admin_lists import event_union, filtered_events

HISTORY_RANGES = {"1h": (3600, 30), "6h": (6 * 3600, 120), "24h": (24 * 3600, 600), "7d": (7 * 86400, 3600)}
THROUGHPUT_RANGES = {"24h": (86400, 3600), "7d": (7 * 86400, 6 * 3600), "30d": (30 * 86400, 86400)}
_DB_RATE_KEYS = {"commit": "xact_commit", "rollback": "xact_rollback", "returned": "tup_returned", "fetched": "tup_fetched",
                 "inserted": "tup_inserted", "updated": "tup_updated", "deleted": "tup_deleted"}
_FAILED = ("failed", "timeout")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def epoch_bucket(db: Session, column, step_seconds: int, offset_seconds: int = 0):
    """SQL expression: floor((epoch(column) - offset) / step), PostgreSQL or SQLite."""
    if db.get_bind().dialect.name == "postgresql":
        return cast(func.floor((func.extract("epoch", column) - offset_seconds) / step_seconds), Integer)
    return cast((cast(func.strftime("%s", column), Integer) - offset_seconds) / step_seconds, Integer)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, ceil(len(ordered) * fraction) - 1))], 1)


# ---------------------------------------------------------------- metric history

def _flatten_frame(frame: dict) -> dict[str, float]:
    point: dict[str, float] = {}
    for group, counts in (frame.get("tasks") or {}).items():
        point[f"{group}_running"] = counts.get("running", 0)
        point[f"{group}_queued"] = counts.get("queued", 0)
    batch = frame.get("tts_batch")
    if batch:  # absent in samples taken before batch pools were reported
        point.update(tts_batch_pools=batch.get("pools", 0), tts_batch_active=batch.get("active_members", 0),
                     tts_batch_parked=batch.get("parked_members", 0), tts_batch_queued=batch.get("queued_members", 0))
    workers = frame.get("workers") or {}
    point.update(slots_active=workers.get("active_slots", 0), slots_total=workers.get("total_slots", 0),
                 workers_online=workers.get("online_workers", 0))
    if workers.get("llm_gate_active") is not None:  # absent in samples taken before the Worker reported it
        point["llm_gate_active"] = workers["llm_gate_active"]
    for key in ("llm_limit", "llm_task_limit"):
        if frame.get(key) is not None:
            point[key] = frame[key]
    database = frame.get("db") or {}
    connections = database.get("connections") or {}
    for key in ("active", "idle", "idle_in_transaction", "total", "max"):
        if key in connections:
            point[f"db_conn_{key}"] = connections[key]
    if database.get("size_bytes") is not None:
        point["db_size_bytes"] = database["size_bytes"]
    redis = frame.get("redis") or {}
    if redis.get("available"):
        point.update(queue_length=redis.get("length", 0), queue_pending=redis.get("pending", 0))
        for key in ("ops_per_sec", "used_memory_bytes", "connected_clients"):
            if redis.get(key) is not None:
                point[f"redis_{key}"] = redis[key]
    host = frame.get("host") or {}
    if host.get("cpu_percent") is not None:
        point["cpu_percent"] = host["cpu_percent"]
    if host.get("ram_used_bytes") and host.get("ram_total_bytes"):
        point["ram_percent"] = round(host["ram_used_bytes"] / host["ram_total_bytes"] * 100, 1)
    if host.get("disk_used_bytes") and host.get("disk_total_bytes"):
        point["disk_percent"] = round(host["disk_used_bytes"] / host["disk_total_bytes"] * 100, 1)
    for gpu in frame.get("gpu") or []:
        index = gpu.get("index", 0)
        if gpu.get("utilization_percent") is not None:
            point[f"gpu{index}_util"] = gpu["utilization_percent"]
        if gpu.get("memory_used_mb") is not None and gpu.get("memory_total_mb"):
            point[f"gpu{index}_mem_percent"] = round(gpu["memory_used_mb"] / gpu["memory_total_mb"] * 100, 1)
        if gpu.get("temperature_c") is not None:
            point[f"gpu{index}_temp"] = gpu["temperature_c"]
    return point


def _db_rates(previous: dict, current: dict, seconds: float) -> dict[str, float]:
    before = ((previous.get("db") or {}).get("counters") or {})
    after = ((current.get("db") or {}).get("counters") or {})
    if not before or not after or seconds <= 0:
        return {}
    if any(after.get(key, 0) < before.get(key, 0) for key in _DB_RATE_KEYS.values()):
        return {}  # statistics were reset between the samples
    rates = {f"db_{name}_ps": (after.get(key, 0) - before.get(key, 0)) / seconds for name, key in _DB_RATE_KEYS.items()}
    hit = after.get("blks_hit", 0) - before.get("blks_hit", 0)
    read = after.get("blks_read", 0) - before.get("blks_read", 0)
    if hit + read > 0:
        rates["db_cache_hit_percent"] = hit / (hit + read) * 100
    return rates


def metrics_history(db: Session, range_name: str, now: datetime | None = None) -> dict:
    span, step = HISTORY_RANGES.get(range_name, HISTORY_RANGES["1h"])
    moment = now or utcnow()
    start = moment - timedelta(seconds=span)
    rows = db.execute(
        select(SystemMetricSample.sampled_at, SystemMetricSample.data)
        .where(SystemMetricSample.sampled_at >= start - timedelta(seconds=90))
        .order_by(SystemMetricSample.bucket)
    ).all()
    buckets: dict[int, list[dict[str, float]]] = defaultdict(list)
    previous: tuple[datetime, dict] | None = None
    for sampled_at, frame in rows:
        sampled_at = _aware(sampled_at)
        point = _flatten_frame(frame or {})
        if previous is not None:
            seconds = (sampled_at - previous[0]).total_seconds()
            if seconds <= 5 * 30:  # a longer gap means the sampler was down
                point.update(_db_rates(previous[1], frame or {}, seconds))
        previous = (sampled_at, frame or {})
        if sampled_at >= start:
            buckets[int(sampled_at.timestamp() // step)].append(point)
    points = []
    first = int(start.timestamp() // step) + 1
    for index in range(first, int(moment.timestamp() // step) + 1):
        samples = buckets.get(index)
        entry: dict = {"time": datetime.fromtimestamp(index * step, timezone.utc).isoformat()}
        if samples:
            for key in {key for sample in samples for key in sample}:
                values = [sample[key] for sample in samples if key in sample]
                entry[key] = round(mean(values), 2)
        points.append(entry)
    latest = rows[-1] if rows else None
    return {
        "range": range_name if range_name in HISTORY_RANGES else "1h",
        "step_seconds": step,
        "points": points,
        "sample_count": sum(len(items) for items in buckets.values()),
        "latest_at": _aware(latest[0]).isoformat() if latest else None,
        "latest": latest[1] if latest else None,
        "scope": "Worker 每 30 秒采样一次并落库，保留 7 天；按区间取平均降采样",
    }


# ---------------------------------------------------------------- throughput

def _series_frame(start: datetime, moment: datetime, step: int, offset: int) -> list[int]:
    first = int((start.timestamp() - offset) // step)
    last = int((moment.timestamp() - offset) // step)
    return list(range(first, last + 1))


def throughput(db: Session, range_name: str, tz_offset_minutes: int = 0, now: datetime | None = None) -> dict:
    span, step = THROUGHPUT_RANGES.get(range_name, THROUGHPUT_RANGES["24h"])
    moment = now or utcnow()
    start = moment - timedelta(seconds=span)
    previous_start = start - timedelta(seconds=span)
    # JS getTimezoneOffset() is UTC - local; aligning on it puts daily bars on local midnight.
    offset = -max(-840, min(int(tz_offset_minutes), 840)) * 60
    frame = _series_frame(start, moment, step, offset)
    series = {index: {"submitted": 0, "succeeded": 0, "failed": 0, "tts_chars": 0, "llm_chars": 0,
                      **{f"{group}_submitted": 0 for group in WORKER_GROUP_NAMES}} for index in frame}

    created_bucket = epoch_bucket(db, Task.created_at, step, offset)
    for task_type, bucket, count in db.execute(
        select(Task.task_type, created_bucket, func.count()).where(Task.created_at >= start)
        .group_by(Task.task_type, created_bucket)
    ).all():
        if bucket in series:
            series[bucket]["submitted"] += int(count)
            series[bucket][f"{task_worker_group(str(task_type))}_submitted"] += int(count)
    finished_bucket = epoch_bucket(db, Task.finished_at, step, offset)
    for status, bucket, count in db.execute(
        select(Task.status, finished_bucket, func.count())
        .where(Task.finished_at >= start, Task.status.in_(("succeeded",) + _FAILED))
        .group_by(Task.status, finished_bucket)
    ).all():
        if bucket in series:
            series[bucket]["succeeded" if status == "succeeded" else "failed"] += int(count)
    usage_bucket = epoch_bucket(db, QuotaTransaction.created_at, step, offset)
    for resource, bucket, chars in db.execute(
        select(QuotaTransaction.resource_type, usage_bucket, func.coalesce(func.sum(QuotaTransaction.char_count), 0))
        .where(QuotaTransaction.created_at >= start, QuotaTransaction.kind == "consume",
               QuotaTransaction.resource_type.in_(("TTS", "LLM")))
        .group_by(QuotaTransaction.resource_type, usage_bucket)
    ).all():
        if bucket in series:
            series[bucket]["tts_chars" if resource == "TTS" else "llm_chars"] += int(chars or 0)

    def window_totals(lower: datetime, upper: datetime) -> dict:
        def count(*conditions) -> int:
            return int(db.scalar(select(func.count()).select_from(Task).where(*conditions)) or 0)

        def chars(resource: str) -> int:
            return int(db.scalar(select(func.coalesce(func.sum(QuotaTransaction.char_count), 0)).where(
                QuotaTransaction.created_at >= lower, QuotaTransaction.created_at < upper,
                QuotaTransaction.kind == "consume", QuotaTransaction.resource_type == resource)) or 0)
        return {
            "submitted": count(Task.created_at >= lower, Task.created_at < upper),
            "succeeded": count(Task.finished_at >= lower, Task.finished_at < upper, Task.status == "succeeded"),
            "failed": count(Task.finished_at >= lower, Task.finished_at < upper, Task.status.in_(_FAILED)),
            "tts_chars": chars("TTS"), "llm_chars": chars("LLM"),
        }

    # Per-type latency on a bounded recent sample keeps percentiles dialect-neutral.
    recent = db.execute(
        select(Task.task_type, Task.status, Task.created_at, Task.started_at, Task.finished_at)
        .where(Task.created_at >= start).order_by(Task.created_at.desc()).limit(5000)
    ).all()
    per_type: dict[str, dict] = {}
    for task_type, status, created_at, started_at, finished_at in recent:
        row = per_type.setdefault(task_type, {"task_type": task_type, "group": task_worker_group(task_type), "total": 0,
                                              "succeeded": 0, "failed": 0, "durations": [], "waits": []})
        row["total"] += 1
        row["succeeded"] += status == "succeeded"
        row["failed"] += status in _FAILED
        if started_at is not None:
            row["waits"].append(max(0.0, (_aware(started_at) - _aware(created_at)).total_seconds()))
            if finished_at is not None and status == "succeeded":
                row["durations"].append(max(0.0, (_aware(finished_at) - _aware(started_at)).total_seconds()))
    by_type = []
    for row in sorted(per_type.values(), key=lambda item: item["total"], reverse=True):
        durations, waits = row.pop("durations"), row.pop("waits")
        finished = row["succeeded"] + row["failed"]
        by_type.append({**row, "success_rate": round(row["succeeded"] / finished, 4) if finished else None,
                        "duration_avg_s": round(mean(durations), 1) if durations else None,
                        "duration_p50_s": _percentile(durations, .5), "duration_p95_s": _percentile(durations, .95),
                        "wait_p50_s": _percentile(waits, .5), "wait_p95_s": _percentile(waits, .95)})

    errors = [
        {"code": code or status, "count": int(count), "sample": sample or ""}
        for code, status, count, sample in db.execute(
            select(Task.error_code, func.min(Task.status), func.count(), func.max(Task.error_message))
            .where(Task.finished_at >= start, Task.status.in_(_FAILED))
            .group_by(Task.error_code).order_by(func.count().desc()).limit(8)
        ).all()
    ]

    # 7 x 24 submission heatmap over the last 30 days, in the console's timezone.
    heat_start = moment - timedelta(days=30)
    hour_bucket = epoch_bucket(db, Task.created_at, 3600, offset)
    heatmap = [[0] * 24 for _ in range(7)]
    for bucket, count in db.execute(
        select(hour_bucket, func.count()).where(Task.created_at >= heat_start).group_by(hour_bucket)
    ).all():
        local = datetime.fromtimestamp(int(bucket) * 3600, timezone.utc)
        heatmap[local.weekday()][local.hour] += int(count)

    return {
        "range": range_name if range_name in THROUGHPUT_RANGES else "24h",
        "step_seconds": step,
        "points": [{"time": datetime.fromtimestamp(index * step - offset, timezone.utc).isoformat(), **series[index]}
                   for index in frame],
        "totals": window_totals(start, moment + timedelta(seconds=1)),
        "previous_totals": window_totals(previous_start, start),
        "by_type": by_type,
        "top_errors": errors,
        "heatmap": heatmap,
        "generated_at": moment.isoformat(),
    }


def today_chars(db: Session, since: datetime) -> dict:
    rows = dict(db.execute(
        select(QuotaTransaction.resource_type, func.coalesce(func.sum(QuotaTransaction.char_count), 0))
        .where(QuotaTransaction.created_at >= since, QuotaTransaction.kind == "consume",
               QuotaTransaction.resource_type.in_(("TTS", "LLM")))
        .group_by(QuotaTransaction.resource_type)
    ).all())
    return {"tts": int(rows.get("TTS", 0) or 0), "llm": int(rows.get("LLM", 0) or 0)}


# ---------------------------------------------------------------- users

def user_counts(db: Session, now: datetime | None = None) -> dict:
    moment = now or utcnow()
    return {
        "admins": int(db.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0),
        "enabled": int(db.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True))) or 0),
        "new_7d": int(db.scalar(select(func.count()).select_from(User).where(User.created_at >= moment - timedelta(days=7))) or 0),
        "active_15m": int(db.scalar(select(func.count(func.distinct(UserSession.user_id))).where(
            UserSession.revoked_at.is_(None), UserSession.expires_at > moment,
            UserSession.last_seen_at >= moment - timedelta(minutes=15))) or 0),
    }


def user_daily_usage(db: Session, user_id: str, days: int = 30, tz_offset_minutes: int = 0, now: datetime | None = None) -> list[dict]:
    moment = now or utcnow()
    offset = -max(-840, min(int(tz_offset_minutes), 840)) * 60
    start = moment - timedelta(days=days)
    frame = _series_frame(start, moment, 86400, offset)[1:]
    series = {index: {"tts_chars": 0, "llm_chars": 0, "tasks": 0} for index in frame}
    day = epoch_bucket(db, QuotaTransaction.created_at, 86400, offset)
    for resource, bucket, chars in db.execute(
        select(QuotaTransaction.resource_type, day, func.coalesce(func.sum(QuotaTransaction.char_count), 0))
        .where(QuotaTransaction.user_id == user_id, QuotaTransaction.created_at >= start,
               QuotaTransaction.kind == "consume", QuotaTransaction.resource_type.in_(("TTS", "LLM")))
        .group_by(QuotaTransaction.resource_type, day)
    ).all():
        if bucket in series:
            series[bucket]["tts_chars" if resource == "TTS" else "llm_chars"] += int(chars or 0)
    task_day = epoch_bucket(db, Task.created_at, 86400, offset)
    for bucket, count in db.execute(
        select(task_day, func.count()).where(Task.owner_id == user_id, Task.created_at >= start).group_by(task_day)
    ).all():
        if bucket in series:
            series[bucket]["tasks"] += int(count)
    return [{"time": datetime.fromtimestamp(index * 86400 - offset, timezone.utc).isoformat(), **series[index]} for index in frame]


# ---------------------------------------------------------------- events

def event_stats(db: Session, hours: int, recent_errors: list[dict], level: str = "all", module: str = "all",
                search: str = "", tz_offset_minutes: int = 0, now: datetime | None = None) -> dict:
    hours = max(1, min(hours, 24 * 30))
    moment = now or utcnow()
    step = 300 if hours <= 1 else 3600 if hours <= 24 else 6 * 3600
    offset = -max(-840, min(int(tz_offset_minutes), 840)) * 60
    events = filtered_events(event_union(hours, recent_errors), level, module, search).subquery()
    bucket = epoch_bucket(db, events.c.time, step, offset)
    frame = _series_frame(moment - timedelta(hours=hours), moment, step, offset)
    series = {index: {"error": 0, "info": 0} for index in frame}
    for level_name, index, count in db.execute(
        select(events.c.level, bucket, func.count()).group_by(events.c.level, bucket)
    ).all():
        if index in series and level_name in series[index]:
            series[index][level_name] += int(count)
    modules = [{"module": name, "count": int(count)} for name, count in db.execute(
        select(events.c.module, func.count()).group_by(events.c.module).order_by(func.count().desc())
    ).all()]
    return {
        "step_seconds": step,
        "points": [{"time": datetime.fromtimestamp(index * step - offset, timezone.utc).isoformat(), **series[index]} for index in frame],
        "modules": modules,
        "total": sum(item["count"] for item in modules),
    }
