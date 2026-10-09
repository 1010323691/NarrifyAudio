"""Best-effort host, GPU, Redis and database probes for the admin console.

Shared by the admin API (live snapshots) and the Worker metrics sampler
(persisted history). Every probe degrades to ``None``/empty values instead of
raising: a missing tool or service is an operational fact to display, not a
request failure.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session


def memory_usage(host_total_bytes: int, cgroup_root: Path = Path("/sys/fs/cgroup")) -> tuple[int, int]:
    """Prefer the current container's cgroup memory usage/limit when available."""
    for current_name, limit_name in (("memory.current", "memory.max"), ("memory/memory.usage_in_bytes", "memory/memory.limit_in_bytes")):
        try:
            current = int((cgroup_root / current_name).read_text().strip())
            raw_limit = (cgroup_root / limit_name).read_text().strip()
            limit = int(raw_limit) if raw_limit != "max" else host_total_bytes
            if current >= 0 and 0 < limit < host_total_bytes * 2:
                return min(current, limit), limit
        except (OSError, ValueError):
            continue
    import psutil
    memory = psutil.virtual_memory()
    return memory.used, memory.total


def host_metrics(storage_root: Path | None = None, *, cpu_interval: float = 0.1) -> dict:
    result: dict = {"cpu_percent": None, "ram_used_bytes": None, "ram_total_bytes": None,
                    "disk_used_bytes": None, "disk_total_bytes": None, "disk_free_bytes": None,
                    "uptime_seconds": None, "load_average_1m": None}
    if storage_root is not None:
        try:
            disk = shutil.disk_usage(storage_root)
            result.update(disk_used_bytes=disk.used, disk_total_bytes=disk.total, disk_free_bytes=disk.free)
        except OSError:
            pass
    if hasattr(os, "getloadavg"):
        try:
            result["load_average_1m"] = round(os.getloadavg()[0], 2)
        except OSError:
            pass
    try:
        import psutil
        ram_used, ram_total = memory_usage(psutil.virtual_memory().total)
        result.update(cpu_percent=psutil.cpu_percent(interval=cpu_interval), ram_used_bytes=ram_used,
                      ram_total_bytes=ram_total, uptime_seconds=int(time.time() - psutil.boot_time()))
    except ImportError:
        pass
    return result


# The Admin page auto-refreshes every ~15 s; each poll reads /overview and
# /performance, and both take this per-process TTL-cached sample — the TTL
# covers a full refresh interval, so a normal poll costs at most one
# nvidia-smi spawn and idle polls within the window cost none (Q20).
GPU_SAMPLE_TTL_SECONDS = 20.0
_gpu_sample_lock = threading.Lock()
_gpu_sample: tuple[float, list[dict]] | None = None


def sample_gpus() -> list[dict]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return []
    try:
        result = subprocess.run(
            [binary, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode != 0:
            result = subprocess.run(
                [binary, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, check=True,
            )

        def numeric(value: str, *, integer: bool = True):
            try:
                parsed = float(value)
                return int(parsed) if integer else round(parsed, 1)
            except (TypeError, ValueError):
                return None

        rows = []
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 6)]
            if len(parts) not in {6, 7}:
                continue
            index = numeric(parts[0])
            if index is None:
                continue
            rows.append({
                "index": index,
                "name": parts[1],
                "utilization_percent": numeric(parts[2]),
                "memory_used_mb": numeric(parts[3]),
                "memory_total_mb": numeric(parts[4]),
                "temperature_c": numeric(parts[5]),
                "power_w": numeric(parts[6], integer=False) if len(parts) == 7 else None,
            })
        return rows
    except (OSError, subprocess.SubprocessError):
        return []


def gpu_status() -> list[dict]:
    """The TTL-cached sample, single-flighted: the spawn runs UNDER the lock,
    so concurrent callers racing a stale sample all wait for and share one
    spawn (the subprocess's 3 s timeout bounds how long the lock is held)."""
    global _gpu_sample
    with _gpu_sample_lock:
        if _gpu_sample is not None and time.monotonic() - _gpu_sample[0] < GPU_SAMPLE_TTL_SECONDS:
            return _gpu_sample[1]
        rows = sample_gpus()
        _gpu_sample = (time.monotonic(), rows)
        return rows


def queue_status(*, include_info: bool = False) -> dict:
    import redis

    from .outbox import STREAM_NAME

    client = redis.Redis.from_url(
        os.getenv("NARRIFY_REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True, socket_timeout=3, socket_connect_timeout=3,
    )
    try:
        client.ping()
        length = int(client.xlen(STREAM_NAME))
        pending = 0
        try:
            summary = client.xpending(STREAM_NAME, os.getenv("NARRIFY_TASK_GROUP", "narrify-workers"))
            pending = int(summary.get("pending", 0)) if isinstance(summary, dict) else int(summary[0] or 0)
        except redis.ResponseError:
            pass
        result: dict = {"available": True, "stream": STREAM_NAME, "length": length, "pending": pending}
        if include_info:
            info = client.info()
            result.update(
                used_memory_bytes=int(info.get("used_memory", 0) or 0),
                connected_clients=int(info.get("connected_clients", 0) or 0),
                ops_per_sec=int(info.get("instantaneous_ops_per_sec", 0) or 0),
            )
        return result
    except Exception as exc:  # Redis is an operational dependency, not a request crash.
        return {"available": False, "stream": STREAM_NAME, "length": 0, "pending": 0, "error": str(exc)}
    finally:
        try:
            client.close()
        except Exception:
            pass


_PG_COUNTERS = ("xact_commit", "xact_rollback", "tup_fetched", "tup_returned", "tup_inserted",
                "tup_updated", "tup_deleted", "blks_hit", "blks_read", "deadlocks")


def database_metrics(db: Session) -> dict | None:
    """PostgreSQL activity for the current database; ``None`` on other dialects.

    ``counters`` are cumulative since the statistics reset — the history
    endpoint converts neighbouring samples into per-second rates.
    """
    if db.get_bind().dialect.name != "postgresql":
        return None
    row = db.execute(text(
        "SELECT " + ", ".join(_PG_COUNTERS) + ", pg_database_size(datname) AS size_bytes "
        "FROM pg_stat_database WHERE datname = current_database()"
    )).mappings().first()
    states = dict(db.execute(text(
        "SELECT coalesce(state, 'unknown'), count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND backend_type = 'client backend' GROUP BY 1"
    )).all())
    max_connections = int(db.scalar(text("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")) or 0)
    counters = {name: int(row[name] or 0) for name in _PG_COUNTERS} if row else {}
    return {
        "counters": counters,
        "size_bytes": int(row["size_bytes"] or 0) if row else None,
        "connections": {
            "active": int(states.get("active", 0)),
            "idle": int(states.get("idle", 0)),
            "idle_in_transaction": int(states.get("idle in transaction", 0)) + int(states.get("idle in transaction (aborted)", 0)),
            "total": int(sum(states.values())),
            "max": max_connections,
        },
    }
