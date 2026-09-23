"""Small, process-local rolling API snapshot for the administrator console."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from math import ceil
from statistics import mean
from threading import Lock
from time import monotonic

_samples: deque[tuple[float, str, str, int, float, str]] = deque(maxlen=5000)
_lock = Lock()


def record_api_request(route: str, status: int, duration_ms: float, method: str = "GET") -> None:
    with _lock:
        _samples.append((monotonic(), method, route, status, duration_ms, datetime.now(timezone.utc).isoformat()))


def api_snapshot(window_seconds: int = 300) -> dict:
    cutoff = monotonic() - window_seconds
    with _lock:
        recent = [row for row in _samples if row[0] >= cutoff]
    durations = sorted(row[4] for row in recent)
    failures = sum(1 for row in recent if row[3] >= 500)
    by_route: dict[str, list[tuple[int, float]]] = {}
    for _, _, route, status, duration, _ in recent:
        by_route.setdefault(route, []).append((status, duration))
    return {
        "window_seconds": window_seconds,
        "request_count": len(recent),
        "server_error_count": failures,
        "error_rate": round(failures / len(recent), 4) if recent else 0,
        "average_ms": round(mean(durations), 1) if durations else None,
        "p95_ms": round(durations[min(len(durations) - 1, ceil(len(durations) * .95) - 1)], 1) if durations else None,
        "status_counts": {
            str(code): sum(1 for row in recent if row[3] == code)
            for code in sorted({row[3] for row in recent})
        },
        "endpoints": [
            {"route": route, "requests": len(rows), "server_errors": sum(1 for status, _ in rows if status >= 500),
             "error_rate": round(sum(1 for status, _ in rows if status >= 500) / len(rows), 4),
             "average_ms": round(mean(duration for _, duration in rows), 1),
             "p95_ms": round(sorted(duration for _, duration in rows)[min(len(rows) - 1, ceil(len(rows) * .95) - 1)], 1)}
            for route, rows in sorted(by_route.items(), key=lambda item: len(item[1]), reverse=True)[:8]
        ],
        "recent_errors": [
            {"time": timestamp, "method": method, "route": route, "status": status}
            for _, method, route, status, _, timestamp in recent if status >= 500
        ][-50:],
        "scope": "当前 API 进程近五分钟；重启后重新计数",
    }
