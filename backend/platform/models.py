from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


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
    active_workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", name="fk_user_sessions_active_workspace_id", ondelete="SET NULL"), index=True
    )

    user: Mapped[User] = relationship(back_populates="sessions")
    __table_args__ = (Index("ix_sessions_active_token", "token_hash", "revoked_at", "expires_at"),)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    owner: Mapped[User] = relationship(back_populates="projects")
    files: Mapped[list["ProjectFile"]] = relationship(back_populates="project")
    tasks: Mapped[list["Task"]] = relationship(back_populates="project")

    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_projects_id_owner"),
        UniqueConstraint("owner_id", "name", "deleted_at", name="uq_projects_owner_name_deleted"),
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


class QuotaReservation(Base):
    __tablename__ = "quota_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), unique=True, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="reserved", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuotaTransaction(Base):
    __tablename__ = "quota_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), index=True)
    reservation_id: Mapped[str | None] = mapped_column(ForeignKey("quota_reservations.id", name="fk_quota_transactions_reservation_id", ondelete="RESTRICT"), index=True)
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


class Workspace(TimestampMixin, Base):
    """A user-owned workspace directory under the administrator's root."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    directory_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (UniqueConstraint("owner_id", "name", "deleted_at", name="uq_workspaces_owner_name_deleted"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
