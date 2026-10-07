"""Text-format workbench endpoints: flow orchestration, aggregated state,
review marks, and version-bound reads/exports.

All reads are scoped to an explicit flow (version) so the UI can never mix
generations of split files: the service validates the stored manifest against
the live artifacts before serving any content.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Query, APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_authenticated_user, require_csrf
from ..platform.models import Project, User
from ..platform.task_submission import TaskSubmissionError
from ..services.task_operations import owned_project
from ..services.text_format_workbench import (
    WorkbenchError,
    flow_state,
    mark_review,
    preview_path,
    start_or_continue_flow,
    unmark_review,
)

from ..services.list_paging import page_text_state

router = APIRouter(prefix="/api/v1/projects", tags=["text-format"])


class FlowRequest(BaseModel):
    source_file_id: str | None = None
    source_file_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
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


@router.post("/{project_id}/text-format/flow")
def post_text_format_flow(project_id: str, body: FlowRequest, user: User = Depends(require_csrf), db: Session = Depends(get_db),
                          page: Annotated[int | None, Query(ge=1)] = None, page_size: Annotated[int, Query(ge=1, le=100)] = 10, q: str = "", filter: str = "all", reason: str = "", orig_num: int | None = None) -> dict:
    item = _owned(db, user, project_id)
    try:
        state = start_or_continue_flow(
            db, user, item.id,
            source_file_id=body.source_file_id, source_file_ids=body.source_file_ids, config=body.config,
            whole_book=body.whole_book, force_by_length=body.force_by_length,
            restart=body.restart,
        )
        return page_text_state(state, page, page_size, q, filter, reason, orig_num) if page is not None else state
    except WorkbenchError as error:
        _raise(error)
        raise
    except TaskSubmissionError as exc:
        # 存储迁移等提交面错误：与旧提交面（task_operations）同等转换为状态码，不冒 500。
        raise HTTPException(exc.status_code, exc.message) from exc


@router.get("/{project_id}/text-format/state")
def get_text_format_state(project_id: str, recover: bool = True, page: Annotated[int | None, Query(ge=1)] = None, page_size: Annotated[int, Query(ge=1, le=100)] = 10, q: str = "", filter: str = "all", reason: str = "", orig_num: int | None = None, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    """Aggregated state. A running flow with no in-flight stage tasks is
    advanced idempotently (stable tflow keys), so the read also recovers
    flows whose last task finished after the client left. Set recover=false
    for a read-only snapshot without advancement or legacy-flow adoption."""
    item = _owned(db, user, project_id)
    try:
        state = flow_state(db, user, item.id, recover=recover)
        return page_text_state(state, page, page_size, q, filter, reason, orig_num) if page is not None else state
    except WorkbenchError as error:
        _raise(error)
        raise
    except TaskSubmissionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc


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
    """Versioned inline chapter read; attachment requests are refused."""
    item = _owned(db, user, project_id)
    if download:
        raise HTTPException(403, "章节文本为制作资料，只提供在线检查，不提供下载。")
    try:
        path = preview_path(db, user, item, flow_id, name, inline=not download)
    except WorkbenchError as error:
        _raise(error)
        raise
    if not path.is_file():
        raise HTTPException(409, {"message": "文件缺失，无法读取"})
    return StreamingResponse(_iter_text(path), media_type="text/plain; charset=utf-8")


@router.get("/{project_id}/text-format/zip")
def get_text_format_zip(project_id: str, flow_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)):
    """Keep the legacy URL explicit, but refuse production-material export."""
    _owned(db, user, project_id)
    raise HTTPException(403, "章节文本为制作资料，不提供打包下载。")


def _iter_text(path, chunk_size: int = 1024 * 1024):
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            yield chunk
