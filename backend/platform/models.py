from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, MetaData, String, Table, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class GPUSchedulerState(Base):
    __tablename__ = "gpu_scheduler_state"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="local")
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class GPURequest(Base):
    __tablename__ = "gpu_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    service: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    owner_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Process identity includes creation time: a reused PID must never be killed.
    process: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    projects: Mapped[list["Project"]] = relationship(back_populates="owner")
    quota_account: Mapped["UserQuotaAccount | None"] = relationship(back_populates="user", uselist=False)
    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user")

    __table_args__ = (CheckConstraint("role in ('user', 'admin')", name="ck_users_role"),)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", name="fk_user_sessions_active_project_id", ondelete="SET NULL"), index=True
    )

    user: Mapped[User] = relationship(back_populates="sessions")
    __table_args__ = (Index("ix_sessions_active_token", "token_hash", "revoked_at", "expires_at"),)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    directory_key: Mapped[str] = mapped_column(String(180), nullable=False)
    last_selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    owner: Mapped[User] = relationship(back_populates="projects")
    files: Mapped[list["ProjectFile"]] = relationship(back_populates="project")
    tasks: Mapped[list["Task"]] = relationship(back_populates="project")

    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_projects_id_owner"),
        UniqueConstraint("owner_id", "name", "deleted_at", name="uq_projects_owner_name_deleted"),
        UniqueConstraint("directory_key", name="uq_projects_directory_key"),
        Index(
            "uq_projects_owner_active_name", "owner_id", "name", unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class ProjectFile(Base):
    __tablename__ = "project_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(index=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(index=True, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(700), unique=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), default="input", nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    project: Mapped[Project] = relationship(back_populates="files")
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "owner_id"], ["projects.id", "projects.owner_id"], name="fk_project_files_project_owner"),
        Index("ix_project_files_scope", "owner_id", "project_id", "deleted_at"),
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(index=True, nullable=False)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project] = relationship(back_populates="tasks")
    attempts: Mapped[list["TaskAttempt"]] = relationship(back_populates="task")
    events: Mapped[list["TaskEvent"]] = relationship(back_populates="task")
    result: Mapped["TaskResult | None"] = relationship(back_populates="task", uselist=False)

    __table_args__ = (
        ForeignKeyConstraint(["project_id", "owner_id"], ["projects.id", "projects.owner_id"], name="fk_tasks_project_owner"),
        Index("ix_tasks_scope_status", "owner_id", "project_id", "status"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_tasks_owner_idempotency"),
        CheckConstraint("progress >= 0 and progress <= 100", name="ck_tasks_progress"),
        CheckConstraint("status in ('pending','queued','running','paused','cancelling','cancelled','succeeded','failed','retrying','timeout')", name="ck_tasks_status"),
    )


class TaskAttempt(Base):
    __tablename__ = "task_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    task: Mapped[Task] = relationship(back_populates="attempts")
    __table_args__ = (UniqueConstraint("task_id", "attempt_no", name="uq_task_attempt_number"),)


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    task: Mapped[Task] = relationship(back_populates="events")
    __table_args__ = (UniqueConstraint("task_id", "sequence", name="uq_task_event_sequence"),)


class TaskResult(Base):
    __tablename__ = "task_results"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    task: Mapped[Task] = relationship(back_populates="result")


class TextFormatFlow(TimestampMixin, Base):
    """Server-side orchestration record for the layout/split pipeline
    (text.format -> book.analyze -> book.split). The stage task ids plus the
    idempotency-key prefix ``tflow:{id}:{stage}`` make every resume
    idempotent; the manifest captured at split success is the version
    anchor for review marks and versioned reads/exports."""

    __tablename__ = "text_format_flows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(index=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(index=True, nullable=False)
    source_file_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Ordered inputs; NULL keeps historical single-source flows compatible.
    source_file_ids: Mapped[list[str] | None] = mapped_column(JSON)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    whole_book: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 强制按字数分册（即使文本能识别出章节）。whole_book 与 force_by_length 同时为真时
    # whole_book 优先（_advance 的分册分支顺序决定）；前端弹窗两者互斥，该组合只可能来自 API 直调。
    force_by_length: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    format_task_id: Mapped[str | None] = mapped_column(String(36))
    analyze_task_id: Mapped[str | None] = mapped_column(String(36))
    split_task_id: Mapped[str | None] = mapped_column(String(36))
    split_mode: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="running", index=True, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    manifest: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)

    __table_args__ = (
        ForeignKeyConstraint(["project_id", "owner_id"], ["projects.id", "projects.owner_id"], name="fk_text_format_flows_project_owner"),
        Index("ix_text_format_flows_project_updated", "project_id", "updated_at"),
        CheckConstraint("status in ('running','ready','failed')", name="ck_text_format_flows_status"),
        CheckConstraint("split_mode in ('smart','by_length','whole_book') or split_mode is null", name="ck_text_format_flows_split_mode"),
    )


class ChapterReviewMark(TimestampMixin, Base):
    """Human review mark bound to one split task (version) and one chapter
    key; marks never carry over across versions."""

    __tablename__ = "chapter_review_marks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(index=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(index=True, nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    chapter_key: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["project_id", "owner_id"], ["projects.id", "projects.owner_id"], name="fk_chapter_review_marks_project_owner"),
        UniqueConstraint("task_id", "chapter_key", name="uq_review_marks_task_chapter"),
        Index("ix_review_marks_project_task", "project_id", "task_id"),
    )


class UserQuotaAccount(Base):
    __tablename__ = "user_quota_accounts"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    available_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    frozen_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consumed_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Persistent round-robin cursor for fair scheduling across users.
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="quota_account")
    __table_args__ = (CheckConstraint("available_units >= 0 and reserved_units >= 0 and frozen_units >= 0 and consumed_units >= 0", name="ck_quota_nonnegative"),)


class QuotaHold(Base):
    __tablename__ = "quota_holds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), index=True, nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(80), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="held", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("uq_quota_hold_attempt_operation", "attempt_id", "operation_type", unique=True),
    )


class QuotaTransaction(Base):
    __tablename__ = "quota_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", name="fk_quota_transactions_actor_user_id", ondelete="SET NULL"), index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    note: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    available_before: Mapped[int | None] = mapped_column(Integer)
    available_after: Mapped[int | None] = mapped_column(Integer)
    reserved_before: Mapped[int | None] = mapped_column(Integer)
    reserved_after: Mapped[int | None] = mapped_column(Integer)
    consumed_before: Mapped[int | None] = mapped_column(Integer)
    consumed_after: Mapped[int | None] = mapped_column(Integer)
    resource_type: Mapped[str | None] = mapped_column(String(10))
    operation_type: Mapped[str | None] = mapped_column(String(80))
    char_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SystemConfig(Base):
    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default="starting", nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    current_task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


# Revision 0002 imports this table object to create/downgrade historical
# schemas. Keep it detached from Base.metadata so current create_all no longer
# recreates the retired Workspace entity.
class _HistoricalWorkspaceTable:
    __table__ = Table(
        "workspaces", MetaData(),
        Column("id", String(36), primary_key=True),
        Column("owner_id", String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        Column("name", String(160), nullable=False),
        Column("directory_key", String(180), nullable=False, unique=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("deleted_at", DateTime(timezone=True), nullable=True),
        UniqueConstraint("owner_id", "name", "deleted_at", name="uq_workspaces_owner_name_deleted"),
        Index("ix_workspaces_owner_id", "owner_id"),
        Index("ix_workspaces_deleted_at", "deleted_at"),
    )


# Migration-only compatibility export. Runtime code uses Project exclusively.
Workspace = _HistoricalWorkspaceTable


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
