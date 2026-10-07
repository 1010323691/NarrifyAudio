"""Entry identity for durable tasks: which rows of the task centre name the
same run's entry, and — per the registry's ``entry_identity`` payload keys —
when a newer run of the same entry supersedes the old terminal rows.

The predicates live here (platform, not the API layer) so the v1 list /
history endpoints and the aggregate stream share one definition: the stream
emits a ``superseded`` frame exactly for the rows ``one_row_per_entry`` hides.
The display label is never an identity key: batch labels are counts / display
text (「音频合成（N 段）」), so different entries of the same shape share one.
A row whose identity keys are all absent / NULL has no subject and never
matches: legacy rows must not be hidden under a guess.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.expression import ColumnElement

from .models import Task
from .task_lifecycle import ACTIVE_TASK_STATUSES
from .task_registry import TASK_TYPES

NO_IDENTITY = "__narrify_no_identity__"


def identity_value(task, key: str):
    """One identity payload key as comparable text; absent / NULL values
    coalesce to a sentinel so two rows both lacking the key compare equal —
    the engine reads ``payload.get(key)`` (missing and explicit null are the
    same "no subject"), so a re-run that moves a key between absent and null
    is still the same entry."""
    return func.coalesce(task.payload[key].as_string(), NO_IDENTITY)


def same_entry_predicate(newer) -> ColumnElement:
    """The two rows name the same entry: same type, and — per the registry's
    ``entry_identity`` — equal identity values in their payloads. Types with
    no identity keys (whole-book operations) share one entry."""
    terms = []
    for spec in TASK_TYPES.values():
        keys = spec.entry_identity
        term = [Task.task_type == spec.name, newer.task_type == spec.name]
        if keys:
            term.append(or_(*[identity_value(Task, key) != NO_IDENTITY for key in keys]))
            term.extend(identity_value(newer, key) == identity_value(Task, key) for key in keys)
        terms.append(and_(*term))
    return or_(*terms)


def one_row_per_entry() -> ColumnElement:
    """Hide terminal rows superseded by a newer run of the same entry; keep
    every active row. A cancelled re-run does not supersede: it is not a
    completed attempt, and hiding the last real record behind it would make
    the entry vanish from the default history (which excludes cancelled)."""
    newer = aliased(Task)
    superseded = exists(
        select(newer.id).where(
            newer.owner_id == Task.owner_id,
            newer.project_id == Task.project_id,
            newer.task_type == Task.task_type,
            same_entry_predicate(newer),
            newer.status != "cancelled",
            or_(
                newer.created_at > Task.created_at,
                and_(newer.created_at == Task.created_at, newer.id > Task.id),
            ),
        )
    )
    return or_(Task.status.in_(ACTIVE_TASK_STATUSES), ~superseded)


def superseded_task_ids(db: Session, task_ids: Iterable[str]) -> set[str]:
    """Among the given rows, the ones currently hidden by the one-row-per-entry
    rule — terminal rows with a newer non-cancelled run of the same entry.
    The aggregate stream uses this to distinguish 「被新运行替换」 (emit a
    ``superseded`` frame) from 「aged out of the live window」 (stay silent)."""
    ids = list(task_ids)
    if not ids:
        return set()
    rows = db.scalars(select(Task.id).where(Task.id.in_(ids), ~one_row_per_entry())).all()
    return set(rows)


def superseded_ids_hidden_by(
    db: Session, newer_task_ids: Iterable[str], limit: int = 500
) -> set[str]:
    """Terminal rows ``one_row_per_entry`` hides because a newer non-cancelled
    run of the same entry is among ``newer_task_ids`` — regardless of whether
    any stream ever tracked those old rows. The stream window (active + newest
    200) can miss a re-run's predecessor: the 任务中心 preloads up to 500
    ``/history`` rows, so an old row beyond the replay still sits in the
    client's list — name it with a ``superseded`` frame or it stays next to
    its replacement. Newest hidden rows first, capped at the client's preload
    size (a hidden row the client holds can only be one of its newest 500)."""
    ids = list(newer_task_ids)
    if not ids:
        return set()
    newer = aliased(Task)
    hidden_by = exists(
        select(newer.id).where(
            newer.id.in_(ids),
            newer.owner_id == Task.owner_id,
            newer.project_id == Task.project_id,
            newer.task_type == Task.task_type,
            same_entry_predicate(newer),
            newer.status != "cancelled",
            or_(
                newer.created_at > Task.created_at,
                and_(newer.created_at == Task.created_at, newer.id > Task.id),
            ),
        )
    )
    rows = db.scalars(
        select(Task.id)
        .where(Task.status.not_in(ACTIVE_TASK_STATUSES), hidden_by)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(limit)
    ).all()
    return set(rows)


def current_entry_ids(user_id: str, task_types: Iterable[str], project_id: str | None = None):
    """Set-based equivalent of one_row_per_entry for task-center aggregates.

    Rank non-cancelled runs per registry identity, keeping every active row.
    Missing subjects and unregistered types partition by ID, so no guessed
    identity can hide a row. Filter failed rows AFTER ranking: a failed newer
    run still supersedes its predecessor under the shared entry semantics.
    Scope filters are safe before ranking because predecessors must have the
    same owner, project and type. Returning IDs keeps payloads inside SQL.
    """
    types = tuple(task_types)
    specs = [TASK_TYPES[name] for name in types if name in TASK_TYPES]
    width = max((len(spec.entry_identity) for spec in specs), default=0)
    identity_columns = [case(*[
        (Task.task_type == spec.name, identity_value(Task, spec.entry_identity[index]))
        for spec in specs if index < len(spec.entry_identity)
    ], else_=NO_IDENTITY) for index in range(width)]
    has_subject = case(*[
        (Task.task_type == spec.name,
         or_(*[identity_value(Task, key) != NO_IDENTITY for key in spec.entry_identity])
         if spec.entry_identity else True)
        for spec in specs
    ], else_=False) if specs else literal(False)
    unique_subject = case((has_subject, None), else_=Task.id)
    ranked = select(
        Task.id, Task.status,
        func.row_number().over(
            partition_by=[Task.owner_id, Task.project_id, Task.task_type, unique_subject, *identity_columns],
            order_by=[Task.created_at.desc(), Task.id.desc()],
        ).label("entry_rank"),
    ).where(Task.owner_id == user_id, Task.task_type.in_(types), Task.status != "cancelled")
    if project_id is not None:
        ranked = ranked.where(Task.project_id == project_id)
    rows = ranked.subquery("ranked_entries")
    return select(rows.c.id).where(or_(rows.c.status.in_(ACTIVE_TASK_STATUSES), rows.c.entry_rank == 1))
