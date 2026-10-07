"""Script-parse workbench endpoints: project-scoped chapter state,
version-bound batch submission, and project-bound result reads.

Every request is scoped to an explicit project id — the page never depends on
the active-workspace pointer, so a version can go stale without a request
silently reading a different project's or generation's files.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Query, APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_authenticated_user, require_csrf
from ..platform.models import Project, User
from ..services.script_parse_state import (
    ScriptParseError,
    get_state,
    result_file,
    submit_run,
)
from ..services.task_operations import owned_project
from .script import ParseChecks

router = APIRouter(prefix="/api/v1/projects", tags=["script-parse"])


class RunFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Expected current digest (from GET /state). Omitted = legacy file without
    # a table row yet; the server then computes the digest on submit.
    sha256: str | None = None


class RunRequest(BaseModel):
    files: list[RunFile] = Field(min_length=1)
    checks: ParseChecks | None = None


def _owned(db: Session, user: User, project_id: str) -> Project:
    item = owned_project(db, user.id, project_id)
    if item is None:
        raise HTTPException(404, "项目不存在")
    return item


def _raise(error: ScriptParseError) -> None:
    detail: dict = {"message": error.message}
    detail.update(error.detail)
    raise HTTPException(error.status_code, detail=detail)


@router.get("/{project_id}/script-parse/state")
def get_script_parse_state(
    project_id: str,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 10,
    q: str = "", filter: str = "all", keys_only: bool = False,
    user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict:
    """Read-only aggregate: chapter list + per-chapter latest task, latest
    result and result availability. Deliberately unlike the text-format state
    endpoint — it performs NO flow advance / adopt side effects, so polling
    it (e.g. while a parse batch runs) is always safe."""
    item = _owned(db, user, project_id)
    try:
        return get_state(db, user, item.id, page=page, page_size=page_size, query=q, filter=filter, keys_only=keys_only)
    except ScriptParseError as error:
        _raise(error)
        raise


@router.get("/{project_id}/script-parse/summary")
def get_script_parse_summary(project_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    try:
        return get_state(db, user, project_id, summary_only=True)
    except ScriptParseError as error:
        _raise(error)
        raise


@router.post("/{project_id}/script-parse/run")
def post_script_parse_run(
    project_id: str,
    body: RunRequest,
    user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    """Version-bound batch submit: one parse task per file, each carrying the
    digest the page saw (or a server-computed one for legacy files). 409 with
    a machine-readable list when digests changed, a split is publishing, or a
    parse of the same file is already in flight."""
    item = _owned(db, user, project_id)
    try:
        checks = body.checks.model_dump(exclude_none=True) if body.checks is not None else None
        return submit_run(db, user, item.id, [f.model_dump() for f in body.files], checks or None)
    except ScriptParseError as error:
        _raise(error)
        raise


@router.get("/{project_id}/script-parse/results/{file_id}")
def get_script_parse_result(
    project_id: str,
    file_id: str,
    request: Request,
    user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> Response:
    """Project-bound parse-result read. The response is ETagged with the
    artifact sha256, so a same-size re-parse is always detected (the client's
    cache key includes the digest, never the file size)."""
    item = _owned(db, user, project_id)
    try:
        record, path = result_file(db, user, item.id, file_id)
    except ScriptParseError as error:
        _raise(error)
        raise
    etag = f'"{record.sha256}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=path.read_bytes(),
        media_type="application/json",
        headers={"ETag": etag},
    )
