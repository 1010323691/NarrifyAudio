from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import OutboxEvent, QuotaReservation, QuotaTransaction, Task, TaskEvent, UserQuotaAccount, utcnow


TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
# Every status that is not terminal: a task in one of these can still change state.
ACTIVE_TASK_STATUSES = {"pending", "queued", "running", "paused", "cancelling", "retrying"}


def append_task_event(db: Session, task_id: str, event_type: str, payload: dict) -> TaskEvent:
    """Append an ordered event while the caller holds the task row lock."""
    last_sequence = db.scalar(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task_id)
    ) or 0
    event = TaskEvent(
        task_id=task_id,
        sequence=int(last_sequence) + 1,
        event_type=event_type,
        payload=payload,
    )
    db.add(event)
    # SessionLocal disables autoflush. Recovery can append multiple events in
    # one transaction, so persist this sequence before the next max() lookup.
    db.flush()
    return event


def suppress_pending_dispatch(db: Session, task_id: str) -> int:
    """Suppress not-yet-published dispatch events for a cancelled task."""
    result = db.query(OutboxEvent).filter(
        OutboxEvent.aggregate_type == "task",
        OutboxEvent.aggregate_id == task_id,
        OutboxEvent.published_at.is_(None),
    ).update({OutboxEvent.published_at: utcnow()}, synchronize_session=False)
    return int(result or 0)


def _locked_quota(db: Session, user_id: str) -> UserQuotaAccount:
    account = db.scalar(
        select(UserQuotaAccount).where(UserQuotaAccount.user_id == user_id).with_for_update()
    )
    if account is None:
        raise ValueError("用户额度账户不存在")
    return account


def settle_reservation(db: Session, task: Task, *, note: str = "") -> bool:
    """Move a task's reserved units into settled consumption exactly once."""
    reservation = db.scalar(
        select(QuotaReservation).where(QuotaReservation.task_id == task.id).with_for_update()
    )
    if reservation is None or reservation.status != "reserved":
        return False
    account = _locked_quota(db, task.owner_id)
    if account.reserved_units < reservation.units:
        raise ValueError("额度预留账本不一致")
    before_reserved = account.reserved_units
    before_consumed = account.consumed_units
    before_available = account.available_units
    account.reserved_units -= reservation.units
    account.consumed_units += reservation.units
    reservation.status = "settled"
    reservation.settled_at = utcnow()
    db.add(
        QuotaTransaction(
            user_id=task.owner_id,
            task_id=task.id,
            reservation_id=reservation.id,
            amount=reservation.units,
            kind="settle",
            idempotency_key=f"settle:{task.id}",
            note=note or "task succeeded",
            available_before=before_available,
            available_after=account.available_units,
            reserved_before=before_reserved,
            reserved_after=account.reserved_units,
            consumed_before=before_consumed,
            consumed_after=account.consumed_units,
        )
    )
    return True


def release_reservation(db: Session, task: Task, *, kind: str = "release", note: str = "") -> bool:
    """Return a task's reservation to available balance exactly once."""
    reservation = db.scalar(
        select(QuotaReservation).where(QuotaReservation.task_id == task.id).with_for_update()
    )
    if reservation is None or reservation.status != "reserved":
        return False
    account = _locked_quota(db, task.owner_id)
    if account.reserved_units < reservation.units:
        raise ValueError("额度预留账本不一致")
    before_available = account.available_units
    before_reserved = account.reserved_units
    before_consumed = account.consumed_units
    account.reserved_units -= reservation.units
    account.available_units += reservation.units
    reservation.status = "released"
    reservation.settled_at = utcnow()
    db.add(
        QuotaTransaction(
            user_id=task.owner_id,
            task_id=task.id,
            reservation_id=reservation.id,
            amount=reservation.units,
            kind=kind,
            idempotency_key=f"{kind}:{task.id}",
            note=note or "task did not complete",
            available_before=before_available,
            available_after=account.available_units,
            reserved_before=before_reserved,
            reserved_after=account.reserved_units,
            consumed_before=before_consumed,
            consumed_after=account.consumed_units,
        )
    )
    return True
