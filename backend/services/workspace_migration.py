"""Offline, repeatable migration of existing UUID workspaces.

Stop API and Workers, back up the database, then run:
    python -m backend.services.workspace_migration
Use the same NARRIFY_* environment as the application.
"""
from pathlib import Path

from sqlalchemy import select
from ..platform.database import SessionLocal
from ..platform.models import Project, Task
from ..platform.storage import configured_storage_root, lock_storage_migration
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..platform.workspace_layout import (
    PROJECT_DIRECTORIES, named_directory_key, recover_layout_moves, relocate_project,
)


def migrate_workspaces() -> int:
    count = 0
    with SessionLocal.begin() as db:
        if not lock_storage_migration(db):
            raise RuntimeError("工作空间正在使用，请先停止 API 和 Worker")
        if db.scalar(select(Task.id).where(Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1)):
            raise RuntimeError("存在未完成任务，请完成或取消任务后再迁移")
        recover_layout_moves(db)
        for project in db.scalars(select(Project).order_by(Project.created_at)).all():
            if project.deleted_at is None:
                key = named_directory_key(db, project.owner.username, project.name, project_id=project.id)
            elif Path(project.directory_key).name.startswith(f"{project.name[:140]}（回收站"):
                key = project.directory_key
            else:
                for number in range(1, 1001):
                    try:
                        key = named_directory_key(
                            db, project.owner.username, f"{project.name[:140]}（回收站 {number}）", project_id=project.id,
                        )
                        break
                    except ValueError:
                        if number == 1000:
                            raise
            path = configured_storage_root(db) / project.directory_key
            unexpected = path.is_dir() and any(entry.name not in PROJECT_DIRECTORIES for entry in path.iterdir())
            legacy_input = path.is_dir() and any(entry.is_dir() for entry in (path / "01_input").glob("*"))
            if key != project.directory_key or unexpected or legacy_input:
                relocate_project(db, project, key, normalize=True)
                db.flush()
                count += 1
    return count


if __name__ == "__main__":
    print(f"已迁移 {migrate_workspaces()} 个项目工作空间")
