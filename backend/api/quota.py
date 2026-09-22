from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_user
from ..platform.models import QuotaTransaction, User, UserQuotaAccount

router = APIRouter(prefix="/api/v1/quota", tags=["quota"])


def _account_json(account: UserQuotaAccount) -> dict:
    return {
        "available_units": account.available_units,
        "reserved_units": account.reserved_units,
        "frozen_units": account.frozen_units,
        "consumed_units": account.consumed_units,
    }


@router.get("")
def get_quota(user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    account = db.get(UserQuotaAccount, user.id)
    if account is None:
        return {"available_units": 0, "reserved_units": 0, "frozen_units": 0, "consumed_units": 0}
    return _account_json(account)


@router.get("/transactions")
def list_quota_transactions(user: User = Depends(require_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(QuotaTransaction)
        .where(QuotaTransaction.user_id == user.id)
        .order_by(QuotaTransaction.created_at.desc())
        .limit(200)
    ).all()
    return [
        {
            "id": row.id,
            "task_id": row.task_id,
            "reservation_id": row.reservation_id,
            "actor_user_id": row.actor_user_id,
            "amount": row.amount,
            "kind": row.kind,
            "note": row.note,
            "available_before": row.available_before,
            "available_after": row.available_after,
            "reserved_before": row.reserved_before,
            "reserved_after": row.reserved_after,
            "consumed_before": row.consumed_before,
            "consumed_after": row.consumed_after,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
