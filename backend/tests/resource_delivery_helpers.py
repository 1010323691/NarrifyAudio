"""Production provenance fixtures for delivery-focused integration tests."""
from backend.platform.database import SessionLocal
from backend.platform.models import Task, TaskResult, User, utcnow
from backend.platform.resource_delivery import capture_deliveries
from backend.platform.storage import safe_project_workspace_path


def record_delivery(owner_id, project_id, relative, task_type="bgm.mix"):
    with SessionLocal() as db:
        user = db.get(User, owner_id)
        root = safe_project_workspace_path(db, user.username, project_id)
        path = root / relative
        result = {"path": str(path), "file": path.name, "size": path.stat().st_size}
        result["deliveries"] = capture_deliveries(db, user, project_id, task_type, result)
        assert result["deliveries"]
        task = Task(owner_id=owner_id, project_id=project_id, task_type=task_type, status="succeeded", finished_at=utcnow())
        db.add(task)
        db.flush()
        db.add(TaskResult(task_id=task.id, result=result))
        db.commit()
        return task.id
