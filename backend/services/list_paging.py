"""Small, compatible page envelopes for filesystem and workbench lists."""
from __future__ import annotations


def page_meta(total: int, page: int, page_size: int, counts: dict | None = None) -> dict:
    return {"total": total, "page": page, "page_size": page_size, "counts": counts or {"all": total}}


def page_slice(values, page: int, page_size: int):
    return values[(page - 1) * page_size:page * page_size]


def page_text_state(state: dict, page: int, page_size: int, query: str = "", filter: str = "all", reason: str = "", orig_num: int | None = None) -> dict:
    version = state.get("version")
    if not version:
        return {**state, "pagination": page_meta(0, page, page_size)}
    chapters = version.get("chapters") or []
    marks = set(version.get("review_marks") or [])
    counts = {"all": len(chapters), "pending": sum(bool(c.get("pending") and c.get("key") not in marks) for c in chapters),
              "adjusted": sum(bool(c.get("adjusted")) for c in chapters), "marked": len(marks)}
    reasons = list(dict.fromkeys(r for c in chapters for r in c.get("reasons", []) if r != "kept"))
    pad = max((len(str(c.get("numStr") or c.get("seq") or "")) for c in chapters), default=1)
    q = query.strip().casefold()
    selected = [c for c in chapters if (orig_num is None or c.get("orig_num") == orig_num)
                and (orig_num is not None or filter == "all" or (filter == "pending" and c.get("pending") and c.get("key") not in marks)
                     or (filter == "adjusted" and c.get("adjusted")))
                and (orig_num is not None or not reason or reason in c.get("reasons", []))
                and (not q or q in str(c.get("title", "")).casefold() or q in str(c.get("seq"))
                     or q in str(c.get("numStr") or c.get("seq")).zfill(pad))]
    visible = page_slice(selected, page, page_size)
    files = version.get("files") or []
    # Preserve each original sequence explicitly; arrays are no longer indexed
    # by absolute seq on the client after paging.
    visible = [{**c, "file": files[c.get("seq", 1) - 1] if 0 < c.get("seq", 1) <= len(files) else None,
                "dup_info": _dup_info(chapters, c)} for c in visible]
    matter_ids = {id for c in visible for id in c.get("matters", [])}
    return {**state, "version": {**version, "chapters": visible, "files": [c["file"] for c in visible if c["file"]],
            "matters": [m for m in version.get("matters", []) if m["id"] in matter_ids], "review_marks": [c["key"] for c in visible if c.get("key") in marks],
            "report": {"actions": [], "warnings": [], "removed": []}},
            "pagination": {**page_meta(len(selected), page, page_size, counts), "reasons": reasons, "num_pad": pad}}


def _dup_info(chapters: list[dict], chapter: dict):
    if chapter.get("orig_num") is None:
        return None
    seen = set()
    group = []
    for c in chapters:
        if c.get("orig_num") != chapter["orig_num"]:
            continue
        id = c.get("source_chapter_id") or c.get("key")
        if id not in seen:
            seen.add(id)
            group.append(c)
    if len(group) < 2:
        return None
    return {"count": len(group), "index": next((i + 1 for i, c in enumerate(group) if c.get("key") == chapter.get("key") or (c.get("source_chapter_id") and c.get("source_chapter_id") == chapter.get("source_chapter_id"))), 1)}


def page_enriched(names, enrich, state, page, page_size, query="", filter="all", keys_only=False):
    """Enumerate cheap keys first; enrich only the visible page by default.

    State filtering is explicit: filesystem-derived states cannot be inferred
    from names. Enrichment callbacks should use their fingerprint digest cache.
    Bulk selection is an explicit user action, never a first-load prefetch.
    """
    matching = [n for n in names if query.strip().casefold() in n.casefold()]
    enriched = None
    if filter != "all":
        enriched = [enrich(n) for n in matching]
        enriched = [r for r in enriched if state(r) == filter]
        matching = [r.get("name", r.get("stem")) for r in enriched]
    meta = page_meta(len(matching), page, page_size, {"all": len(names)})
    if keys_only:
        return {"items": [{**r, "work_state": state(r)} for r in (enriched if enriched is not None else [enrich(n) for n in matching])], "pagination": meta}
    rows = page_slice(enriched, page, page_size) if enriched is not None else [enrich(n) for n in page_slice(matching, page, page_size)]
    return {"items": rows, "pagination": meta}


def entry_states(db, ctx, task_types):
    """Only status and identity columns, never task payload/config/log objects."""
    from sqlalchemy import select
    from ..platform.models import Task
    from ..platform.project_context import active_project
    from ..platform.task_identity import current_entry_ids
    from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
    project = active_project(db, ctx.user, ctx.session)
    if project is None:
        return {}
    ids = current_entry_ids(ctx.user.id, task_types, project_id=project.id)
    rows = db.execute(select(Task.status, Task.payload["scripts"], Task.payload["package"].as_string(),
                             Task.payload["stem"].as_string(), Task.payload["chapters"])
                      .where(Task.id.in_(ids), Task.task_type.in_(task_types))
                      .order_by(Task.created_at, Task.id)).all()
    states = {}
    for status, scripts, package, stem, chapters in rows:
        for key in (scripts if isinstance(scripts, list) else chapters if isinstance(chapters, list) else [package or stem]):
            if key and (states.get(key) != "active" or status in ACTIVE_TASK_STATUSES):
                states[key] = "active" if status in ACTIVE_TASK_STATUSES else "failed" if status in {"failed", "timeout"} else "settled"
    return states


def project_page(db, user, page, page_size, query="", trashed=False, filter="all"):
    from sqlalchemy import func, select
    from ..platform.models import Project
    from ..platform.system_config import project_retention
    from .projects import as_utc, project_expires_at, trash_expires_at
    statement = select(Project).where(Project.owner_id == user.id,
                                      Project.deleted_at.is_not(None) if trashed else Project.deleted_at.is_(None))
    if query.strip(): statement = statement.where(Project.name.ilike("%" + query.strip() + "%"))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    # Trash expiry follows the administrator-set number of days.
    if trashed:
        trash_days = project_retention(db)["trash_days"]
        from ..platform.models import utcnow
        projects = db.scalars(statement.order_by(Project.deleted_at, Project.id)).all()
        active = sum(trash_expires_at(p.deleted_at, trash_days) > as_utc(utcnow()) for p in projects)
        projects = [p for p in projects if filter == "all" or (trash_expires_at(p.deleted_at, trash_days) > as_utc(utcnow())) == (filter == "active")]
        total = len(projects)
        projects = page_slice(projects, page, page_size)
    else:
        projects = db.scalars(statement.order_by(Project.updated_at.desc(), Project.id).offset((page-1)*page_size).limit(page_size)).all()
    ttl_days = 0 if trashed else project_retention(db)["project_ttl_days"]
    items = [{"id": p.id, "name": p.name, "description": p.description, "directory_key": p.directory_key,
              "created_at": p.created_at.isoformat(), "updated_at": p.updated_at.isoformat(),
              **({"expires_at": project_expires_at(p, ttl_days).isoformat()} if ttl_days else {}),
              **({"deleted_at": as_utc(p.deleted_at).isoformat(), "expires_at": trash_expires_at(p.deleted_at, trash_days).isoformat()} if trashed else {})} for p in projects]
    return {"items": items, "pagination": page_meta(total, page, page_size, {"all": total, **({"active": active} if trashed else {})})}


def quota_page(db, user, page, page_size, query, filter, tz_offset=0):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import case, func, or_, select
    from ..platform.models import Project, ProjectFile, QuotaTransaction as Q
    statement = select(Q).where(Q.user_id == user.id)
    if query.strip():
        term = "%" + query.strip() + "%"
        statement = statement.where(or_(Q.note.ilike(term), Q.task_id.ilike(term), Q.kind.ilike(term), Q.operation_type.ilike(term)))
    if filter == "consume": statement = statement.where(Q.kind.in_(["consume", "settle"]))
    elif filter != "all": statement = statement.where(Q.kind == filter)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.scalars(statement.order_by(Q.created_at.desc(), Q.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    items = [{key: getattr(r, key) for key in ("id", "task_id", "actor_user_id", "amount", "kind", "note", "available_before", "available_after", "reserved_before", "reserved_after", "consumed_before", "consumed_after", "resource_type", "operation_type", "char_count")} | {"created_at": r.created_at.isoformat()} for r in rows]
    offset = timedelta(minutes=max(-840, min(840, tz_offset)))
    midnight = (datetime.now(timezone.utc) - offset).replace(hour=0, minute=0, second=0, microsecond=0) + offset
    starts = [midnight - timedelta(days=6-i) for i in range(7)]
    daily = db.execute(select(*[func.coalesce(func.sum(case((Q.created_at >= start, case((Q.created_at < start + timedelta(days=1), func.abs(Q.amount)), else_=0)), else_=0)), 0) for start in starts]).where(Q.user_id == user.id, Q.kind.in_(["consume", "settle"]), Q.created_at >= starts[0])).one()
    storage = db.scalar(select(func.coalesce(func.sum(ProjectFile.size_bytes), 0)).join(Project, Project.id == ProjectFile.project_id).where(ProjectFile.owner_id == user.id, ProjectFile.deleted_at.is_(None), Project.deleted_at.is_(None))) or 0
    return {"items": items, "pagination": page_meta(total, page, page_size), "daily": list(daily), "registered_storage_bytes": storage}
