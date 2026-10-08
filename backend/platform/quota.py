"""Central character-based quota accounting for model work."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import QuotaHold, QuotaTransaction, UserQuotaAccount, new_id


class QuotaInsufficientError(RuntimeError):
    """Raised before resource execution when available character quota is insufficient."""


@dataclass(frozen=True)
class QuotaContext:
    user_id: str
    task_id: str
    attempt_id: str


_context: ContextVar[QuotaContext | None] = ContextVar("quota_context", default=None)
_llm_sequence: ContextVar[int] = ContextVar("quota_llm_sequence", default=0)


def set_quota_context(user_id: str, task_id: str, attempt_id: str):
    return _context.set(QuotaContext(user_id, task_id, attempt_id))


def reset_quota_context(token) -> None:
    _context.reset(token)
    _llm_sequence.set(0)


def consume_llm_output(
    output: str, operation_type: str = "llm.operation", *, operation_key: str | None = None,
) -> bool:
    """Charge one accepted business response; malformed retry responses are excluded."""
    context = _context.get()
    if context is None:
        return True
    if operation_key is None:
        sequence = _llm_sequence.get() + 1
        _llm_sequence.set(sequence)
        operation_key = str(sequence)
    require_quota("LLM", operation_type)
    charged = consume_quota(
        "LLM", operation_type, len(output or ""),
        idempotency_key=f"{context.task_id}:llm:{operation_key}",
    )
    if not charged:
        raise QuotaInsufficientError(f"字数额度不足，无法结算{operation_type}（LLM）")
    return True


def reserve_tts_quota(char_count: int, operation_type: str) -> bool:
    context = _context.get()
    if context is None:
        return True
    count = max(0, int(char_count))
    if count == 0:
        return True
    with SessionLocal() as db:
        hold = db.scalar(select(QuotaHold).where(
            QuotaHold.attempt_id == context.attempt_id,
            QuotaHold.operation_type == operation_type,
        ).with_for_update())
        if hold is not None:
            return hold.status == "held"
        account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == context.user_id).with_for_update())
        hold = db.scalar(select(QuotaHold).where(
            QuotaHold.attempt_id == context.attempt_id,
            QuotaHold.operation_type == operation_type,
        ).with_for_update())
        if hold is not None:
            db.rollback()
            return hold.status == "held"
        if account is None or account.available_units < count:
            db.rollback()
            return False
        before = account.available_units
        account.available_units -= count
        account.reserved_units += count
        hold = QuotaHold(
            user_id=context.user_id, task_id=context.task_id, attempt_id=context.attempt_id,
            operation_type=operation_type, units=count,
        )
        db.add(hold)
        db.flush()
        db.add(QuotaTransaction(
            user_id=context.user_id, task_id=context.task_id, amount=-count, kind="reserve",
            idempotency_key=f"hold:{context.attempt_id}:{operation_type}",
            note=f"{operation_type} TTS 输入字数预留", resource_type="TTS",
            operation_type=operation_type, char_count=count,
            available_before=before, available_after=account.available_units,
            reserved_before=account.reserved_units - count, reserved_after=account.reserved_units,
            consumed_before=account.consumed_units, consumed_after=account.consumed_units,
        ))
        db.commit()
    return True


def consume_tts_input(char_count: int, operation_type: str, idempotency_key: str) -> bool:
    return consume_tts_inputs([(char_count, operation_type, idempotency_key)])


def consume_tts_inputs(inputs: list[tuple[int, str, str]]) -> bool:
    """Charge a bounded publication batch in one transaction, retaining per-segment keys.

    Lock holds in operation order before the account, matching release/reserve lock
    order. Recheck keys under these locks so concurrent retries cannot double charge.
    Validation covers the entire batch before any balance is changed.
    """
    context = _context.get()
    if context is None:
        return True
    charges = {}
    for count, operation, identifier in inputs:
        count = max(0, int(count))
        if not count:
            continue
        key = f"{context.task_id}:tts:{identifier}"
        value = (count, operation)
        if key in charges and charges[key] != value:
            raise ValueError("同一 TTS 幂等键的字数或操作不一致")
        charges[key] = value
    if not charges:
        return True
    with SessionLocal() as db:
        def existing_keys():
            return set(db.scalars(select(QuotaTransaction.idempotency_key).where(
                QuotaTransaction.idempotency_key.in_(charges),
            )))
        prior = existing_keys()
        if len(prior) == len(charges):
            return True
        operations = sorted({op for key, (_count, op) in charges.items() if key not in prior})
        holds = {hold.operation_type: hold for hold in db.scalars(select(QuotaHold).where(
            QuotaHold.attempt_id == context.attempt_id,
            QuotaHold.user_id == context.user_id,
            QuotaHold.operation_type.in_(operations),
        ).order_by(QuotaHold.operation_type).with_for_update())}
        account = db.scalar(select(UserQuotaAccount).where(
            UserQuotaAccount.user_id == context.user_id,
        ).with_for_update())
        prior = existing_keys()
        pending = [(key, count, op) for key, (count, op) in charges.items() if key not in prior]
        required = {}
        for _key, count, op in pending:
            required[op] = required.get(op, 0) + count
        if account is None or account.reserved_units < sum(required.values()):
            raise RuntimeError("TTS 字数预留不足，批次未结算")
        for op, count in required.items():
            hold = holds.get(op)
            if hold is None or hold.status != "held" or hold.units < count:
                raise RuntimeError(f"TTS 字数预留不足：{op} 需要 {count} 字")
        for key, count, op in pending:
            hold = holds[op]
            before_consumed = account.consumed_units
            hold.units -= count
            account.reserved_units -= count
            account.consumed_units += count
            db.add(QuotaTransaction(
                user_id=context.user_id, task_id=context.task_id, amount=count, kind="consume",
                idempotency_key=key, note=f"{op}（TTS 输入字数）",
                resource_type="TTS", operation_type=op, char_count=count,
                available_before=account.available_units, available_after=account.available_units,
                reserved_before=account.reserved_units + count, reserved_after=account.reserved_units,
                consumed_before=before_consumed, consumed_after=account.consumed_units,
            ))
            if hold.units == 0:
                hold.status = "consumed"
        db.commit()
    return True


def release_attempt_holds(task_id: str, attempt_id: str, *, db: Session) -> None:
    statement = select(QuotaHold).where(
        QuotaHold.task_id == task_id, QuotaHold.attempt_id == attempt_id, QuotaHold.status == "held",
    ).order_by(QuotaHold.operation_type)
    for hold in db.scalars(statement.with_for_update()).all():
        account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == hold.user_id).with_for_update())
        if account is None:
            continue
        amount = hold.units
        if not amount:
            hold.status = "released"
            continue
        before_available, before_reserved = account.available_units, account.reserved_units
        account.available_units += amount
        account.reserved_units -= amount
        hold.units = 0
        hold.status = "released"
        db.add(QuotaTransaction(
            user_id=hold.user_id, task_id=task_id, amount=amount, kind="release",
            idempotency_key=f"release:{hold.id}", note=f"{hold.operation_type} 未执行字数退回",
            resource_type="TTS", operation_type=hold.operation_type, char_count=amount,
            available_before=before_available, available_after=account.available_units,
            reserved_before=before_reserved, reserved_after=account.reserved_units,
            consumed_before=account.consumed_units, consumed_after=account.consumed_units,
        ))


def check_quota_available(user_id: str, *, db: Session | None = None) -> bool:
    if db is not None:
        account = db.get(UserQuotaAccount, user_id)
        return bool(account and account.available_units > 0)
    with SessionLocal() as session:
        account = session.get(UserQuotaAccount, user_id)
        return bool(account and account.available_units > 0)


def consume_quota(
    resource_type: str,
    operation_type: str,
    char_count: int,
    *,
    idempotency_key: str | None = None,
    task_id: str | None = None,
    user_id: str | None = None,
) -> bool:
    """Atomically charge actual LLM output or successful TTS input characters."""
    count = max(0, int(char_count))
    if count == 0:
        return True
    context = _context.get()
    user_id = user_id or (context.user_id if context else None)
    task_id = task_id or (context.task_id if context else None)
    if not user_id or resource_type not in {"LLM", "TTS"}:
        return False  # non-user maintenance invocation
    key = idempotency_key or f"{task_id or 'direct'}:{resource_type}:{new_id()}"
    with SessionLocal() as db:
        prior = db.scalar(select(QuotaTransaction).where(QuotaTransaction.idempotency_key == key))
        if prior is not None:
            return True
        account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == user_id).with_for_update())
        prior = db.scalar(select(QuotaTransaction).where(QuotaTransaction.idempotency_key == key))
        if prior is not None:
            db.rollback()
            return True
        if account is None or account.available_units < count:
            db.rollback()
            return False
        before_available = account.available_units
        before_consumed = account.consumed_units
        account.available_units -= count
        account.consumed_units += count
        db.add(QuotaTransaction(
            user_id=user_id, task_id=task_id, amount=count, kind="consume",
            idempotency_key=key, note=f"{operation_type}（{resource_type} 字数）",
            resource_type=resource_type, operation_type=operation_type, char_count=count,
            available_before=before_available, available_after=account.available_units,
            reserved_before=account.reserved_units, reserved_after=account.reserved_units,
            consumed_before=before_consumed, consumed_after=account.consumed_units,
        ))
        db.commit()
    return True


def require_quota(resource_type: str, operation_type: str) -> None:
    """Fail before a model request when the account has no spendable characters."""
    context = _context.get()
    if context and resource_type == "TTS":
        with SessionLocal() as session:
            holds = session.scalars(select(QuotaHold).where(
                QuotaHold.attempt_id == context.attempt_id,
                QuotaHold.status == "held",
            )).all()
        if any(hold.operation_type == operation_type or hold.operation_type.startswith(operation_type + ".") for hold in holds):
            return
    if context and not check_quota_available(context.user_id):
        raise QuotaInsufficientError(f"字数额度不足，无法执行{operation_type}（{resource_type}）")


def reserve_tts_quotas(requests: list[tuple[int, str]]) -> dict[str, bool]:
    """Reserve chapter inputs in request order with one account lock/commit."""
    context = _context.get()
    if context is None:
        return {operation: True for _, operation in requests}
    from sqlalchemy import insert
    from .models import utcnow
    result = {}
    with SessionLocal() as db:
        operations = [operation for _, operation in requests]
        holds = {hold.operation_type: hold for hold in db.scalars(select(QuotaHold).where(
            QuotaHold.attempt_id == context.attempt_id, QuotaHold.operation_type.in_(operations)
        ).order_by(QuotaHold.operation_type).with_for_update())}
        account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == context.user_id).with_for_update())
        # Recheck after locking the account, as in the single-reservation path.
        holds.update({hold.operation_type: hold for hold in db.scalars(select(QuotaHold).where(
            QuotaHold.attempt_id == context.attempt_id, QuotaHold.operation_type.in_(operations)))})
        exhausted = False
        new_holds, transactions = [], []
        for chars, operation in requests:
            count = max(0, int(chars))
            if operation in holds:
                result[operation] = holds[operation].status == 'held'
                exhausted |= not result[operation]
                continue
            if not count:
                result[operation] = True
                continue
            if exhausted or account is None or account.available_units < count:
                exhausted = True; result[operation] = False
                continue
            before, reserved = account.available_units, account.reserved_units
            account.available_units -= count; account.reserved_units += count
            result[operation] = True
            new_holds.append(dict(id=new_id(), user_id=context.user_id, task_id=context.task_id,
                                  attempt_id=context.attempt_id, operation_type=operation, units=count, status='held'))
            transactions.append(dict(id=new_id(), user_id=context.user_id, task_id=context.task_id,
                                     amount=-count, kind='reserve', idempotency_key=f'hold:{context.attempt_id}:{operation}',
                                     note=f'{operation} TTS 输入字数预留', resource_type='TTS', operation_type=operation,
                                     char_count=count, available_before=before, available_after=account.available_units,
                                     reserved_before=reserved, reserved_after=account.reserved_units,
                                     consumed_before=account.consumed_units, consumed_after=account.consumed_units))
        for offset in range(0, len(new_holds), 100):
            db.execute(insert(QuotaHold), new_holds[offset:offset + 100])
            db.execute(insert(QuotaTransaction), transactions[offset:offset + 100])
        db.commit()
    return result
