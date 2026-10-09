"""Chart data for the admin console (history, throughput, API latency, event distribution)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..core.observability import api_snapshot, api_timeseries
from ..platform.database import get_db
from ..platform.deps import require_admin
from ..platform.models import User
from ..services.admin_analytics import event_stats, metrics_history, throughput

router = APIRouter(prefix="/api/v1/admin", tags=["admin-analytics"])


@router.get("/metrics/history")
def get_metrics_history(range: str = "1h", _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return metrics_history(db, range)


@router.get("/analytics/throughput")
def get_throughput(range: str = "24h", tz_offset_minutes: int = 0,
                   _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return throughput(db, range, tz_offset_minutes)


@router.get("/analytics/api")
def get_api_series(minutes: Annotated[int, Query(ge=5, le=1440)] = 60, _: User = Depends(require_admin)) -> dict:
    return api_timeseries(minutes)


@router.get("/events/stats")
def get_event_stats(since_hours: int = 24, level: str = "all", module: str = "all", search: str = "",
                    tz_offset_minutes: int = 0, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    recent = api_snapshot(window_seconds=min(300, max(60, since_hours * 60)))["recent_errors"]
    return event_stats(db, since_hours, recent, level, module, search, tz_offset_minutes)
