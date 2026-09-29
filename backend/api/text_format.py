"""Text-format workbench endpoints: flow orchestration, aggregated state,
review marks, and version-bound reads/exports.

All reads are scoped to an explicit flow (version) so the UI can never mix
generations of split files: the service validates the stored manifest against
the live artifacts before serving any content.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_authenticated_user, require_csrf
from ..platform.models import Project, User
from ..services.task_operations import owned_project
from ..services.text_format_workbench import (
    WorkbenchError,
    build_split_zip,
    flow_state,
    mark_review,
    preview_path,
    start_or_continue_flow,
    unmark_review,
)

router = APIRouter(prefix="/api/v1/projects", tags=["text-format"])


class FlowRequest(BaseModel):
    source_file_id: str | None = None
    config: dict = Field(default_factory=dict)
    whole_book: bool = False
    force_by_length: bool = False
    restart: bool = False


class MarkBody(BaseModel):
    task_id: str = Field(min_length=1, max_length=36)
    chapter_key: str = Field(min_length=1, max_length=64)


def _owned(db: Session, user: User, project_id: str) -> Project:
    item = owned_project(db, user.id, project_id)
    if item is None:
        raise HTTPException(404, "项目不存在")
    return item


def _raise(error: WorkbenchError) -> None:
    detail: dict = {"message": error.message}
    detail.update(error.detail)
    raise HTTPException(error.status_code, detail=detail)


def _attachment_disposition(filename: str) -> str:
    """RFC 6266 disposition that stays latin-1 encodable: an ASCII fallback
    plus the RFC 5987 ``filename*`` carrying the real UTF-8 name."""
    parts = [f"filename*=utf-8''{quote(filename, safe='')}"]
    if filename.isascii():
        parts.insert(0, f'filename="{filename}"')
    return "attachment; " + "; ".join(parts)


@router.post("/{project_id}/text-format/flow")
def post_text_format_flow(project_id: str, body: FlowRequest, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, project_id)
    try:
        return start_or_continue_flow(
            db, user, item.id,
            source_file_id=body.source_file_id, config=body.config,
            whole_book=body.whole_book, force_by_length=body.force_by_length,
            restart=body.restart,
        )
    except WorkbenchError as error:
        _raise(error)
        raise


@router.get("/{project_id}/text-format/state")
def get_text_format_state(project_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, project_id)
    try:
        return flow_state(db, user, item.id)
    except WorkbenchError as error:
        _raise(error)
        raise


@router.post("/{project_id}/text-format/review-marks")
def post_review_mark(project_id: str, body: MarkBody, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, project_id)
    try:
        return mark_review(db, user, item.id, body.task_id, body.chapter_key)
    except WorkbenchError as error:
        _raise(error)
        raise


@router.delete("/{project_id}/text-format/review-marks")
def delete_review_mark(project_id: str, task_id: str, chapter_key: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, project_id)
    try:
        return unmark_review(db, user, item.id, task_id, chapter_key)
    except WorkbenchError as error:
        _raise(error)
        raise


@router.get("/{project_id}/text-format/file/{name:path}")
def get_text_format_file(project_id: str, name: str, flow_id: str, download: bool = False, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)):
    """Versioned chapter-text read. ``download=1`` serves it as an
    attachment; otherwise plain text for inline preview (fetch .text())."""
    item = _owned(db, user, project_id)
    try:
        path = preview_path(db, user, item, flow_id, name)
    except WorkbenchError as error:
        _raise(error)
        raise
    if not path.is_file():
        raise HTTPException(409, {"message": "文件缺失，无法读取"})
    if download:
        return FileResponse(path, media_type="text/plain; charset=utf-8", filename=name)
    return StreamingResponse(_iter_text(path), media_type="text/plain; charset=utf-8")


@router.get("/{project_id}/text-format/zip")
def get_text_format_zip(project_id: str, flow_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)):
    """Whole-version export: the complete zip is built only after every
    manifest entry is verified against the live artifacts (P0-06)."""
    item = _owned(db, user, project_id)
    try:
        data, digest = build_split_zip(db, user, item, flow_id)
    except WorkbenchError as error:
        _raise(error)
        raise
    stem = (item.name or "text").strip() or "text"
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={
            "Content-Disposition": _attachment_disposition(f"{stem}_分册.zip"),
            "X-Zip-Sha256": digest,
            "X-Zip-Flow": flow_id,
        },
    )


def _iter_text(path, chunk_size: int = 1024 * 1024):
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            yield chunk
