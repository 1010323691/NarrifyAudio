"""Regression coverage for readable directories, migration, and rollback."""
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.platform.database import Base
from backend.platform.models import Project, ProjectFile, Task, TaskResult, User
from backend.platform import storage, workspace_layout
from backend.platform.storage import project_workspace_path, task_attempt_path
from backend.platform.workspace_layout import PROJECT_DIRECTORIES, relocate_project, recover_layout_moves
from backend.services.projects import create_project, rename_project, move_project_to_trash, restore_project


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "configured_storage_root", lambda _db=None: tmp_path)
    monkeypatch.setattr(workspace_layout, "configured_storage_root", lambda _db=None: tmp_path)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(username="reader", email="reader@example.test", password_hash="unused", display_name="Reader")
        session.add(user)
        session.commit()
        yield session
    engine.dispose()


def new_project(db, name="我的有声书"):
    user = db.scalar(select(User))
    project = create_project(db, owner_id=user.id, username=user.username, name=name)
    db.commit()
    return project


def test_create_and_rename_use_project_name_and_fixed_directories(db, tmp_path):
    project = new_project(db)
    root = project_workspace_path(db, "reader", project.id)
    assert root == tmp_path / "reader" / "我的有声书"
    assert {entry.name for entry in root.iterdir()} == set(PROJECT_DIRECTORIES)
    path = root / "01_input" / "书.txt"
    path.write_text("hello")
    file = ProjectFile(project_id=project.id, owner_id=project.owner_id, original_name=path.name,
                       object_key=f"{project.directory_key}/01_input/书.txt", sha256="a" * 64)
    db.add(file)
    db.commit()
    rename_project(db, project, "新书名")
    db.commit()
    assert not root.exists()
    assert (tmp_path / file.object_key).read_text() == "hello"
    assert project.directory_key == "reader/新书名"
    # A later rollback in the same Session must not undo an earlier commit.
    db.rollback()
    assert (tmp_path / file.object_key).is_file()
    stage = task_attempt_path(db, "reader", project.id, "task", "attempt", "receipt.json")
    assert stage.relative_to(tmp_path / project.directory_key).parts[:2] == ("00_temp", "tasks")


@pytest.mark.parametrize("name", ["../escape", "bad/name", "bad\\name", "CON", "aux.txt", "book.", " ", "bad:name"])
def test_invalid_windows_directory_names_are_rejected(db, name):
    with pytest.raises(ValueError):
        new_project(db, name)


def test_case_insensitive_directory_collision_is_rejected(db):
    new_project(db, "Book")
    with pytest.raises(ValueError):
        new_project(db, "book")


def legacy_project(db, tmp_path):
    user = db.scalar(select(User))
    project = Project(owner_id=user.id, name="旧书", directory_key="reader/legacy-id")
    db.add(project)
    db.flush()
    root = tmp_path / project.directory_key
    old = root / "file-id" / "chapter_analysis.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"source_path": str(root / "01_input" / "source.txt")}))
    file = ProjectFile(project_id=project.id, owner_id=user.id, original_name=old.name,
                       object_key=f"{project.directory_key}/file-id/{old.name}", kind="artifact",
                       size_bytes=old.stat().st_size, sha256=storage.sha256_file(old))
    db.add(file)
    db.flush()
    task = Task(owner_id=user.id, project_id=project.id, task_type="book.analyze", status="succeeded", payload={"path": str(old)})
    db.add(task)
    db.flush()
    result = TaskResult(task_id=task.id, result={"file_id": file.id, "object_key": file.object_key, "path": str(old)})
    db.add(result)
    (root / "03_parsed_json").mkdir()
    (root / "03_parsed_json" / old.name).write_text("existing")
    (root / "00_temp").mkdir()
    (root / "00_temp" / old.name).write_text("existing analysis")
    (root / ".tasks").mkdir()
    (root / ".tasks" / "receipt.json").write_text("{}")
    db.commit()
    return project, file, task, result, old


def test_migration_preserves_versions_rewrites_paths_and_cleans_root(db, tmp_path):
    project, file, task, result, old = legacy_project(db, tmp_path)
    relocate_project(db, project, "reader/旧书", normalize=True)
    db.commit()
    root = tmp_path / project.directory_key
    assert set(entry.name for entry in root.iterdir()) == set(PROJECT_DIRECTORIES)
    assert file.object_key == "reader/旧书/00_temp/chapter_analysis (2).json"
    assert result.result["object_key"] == file.object_key
    assert task.payload["path"] == str(tmp_path / file.object_key)
    assert json.loads((tmp_path / file.object_key).read_text())["source_path"] == str(root / "01_input" / "source.txt")
    assert (root / "03_parsed_json" / "chapter_analysis.json").read_text() == "existing"
    assert (root / "00_temp" / "chapter_analysis.json").read_text() == "existing analysis"
    assert (root / "00_temp" / "tasks" / "receipt.json").is_file()
    assert not old.exists()
    rewritten = tmp_path / file.object_key
    assert file.size_bytes == rewritten.stat().st_size
    assert file.sha256 == storage.sha256_file(rewritten)


def test_database_rollback_restores_original_directory_and_file_bytes(db, tmp_path):
    project, file, task, result, old = legacy_project(db, tmp_path)
    before = old.read_bytes()
    original_size, original_sha = file.size_bytes, file.sha256
    relocate_project(db, project, "reader/旧书", normalize=True)
    db.flush()
    db.rollback()
    assert old.read_bytes() == before
    assert project.directory_key == "reader/legacy-id"
    assert task.payload["path"] == str(old)
    assert file.size_bytes == original_size == old.stat().st_size
    assert file.sha256 == original_sha == storage.sha256_file(old)
    assert not (tmp_path / "reader/旧书").exists()


def test_rename_updates_cataloged_json_fingerprint(db, tmp_path):
    project = new_project(db, "Old")
    path = tmp_path / project.directory_key / "03_parsed_json" / "analysis.json"
    path.write_text(json.dumps({"source": str(path.parent.parent / "01_input" / "book.txt")}))
    file = ProjectFile(project_id=project.id, owner_id=project.owner_id,
                       original_name=path.name, kind="artifact",
                       object_key=path.relative_to(tmp_path).as_posix(),
                       size_bytes=path.stat().st_size, sha256=storage.sha256_file(path))
    db.add(file)
    db.commit()
    old_sha = file.sha256
    rename_project(db, project, "New longer name")
    db.commit()
    rewritten = tmp_path / file.object_key
    assert file.size_bytes == rewritten.stat().st_size
    assert file.sha256 == storage.sha256_file(rewritten) != old_sha
    assert "New longer name" in json.loads(rewritten.read_text())["source"]


def test_trash_frees_name_and_restore_moves_to_readable_collision_name(db, tmp_path):
    project = new_project(db, "同名")
    move_project_to_trash(db, project)
    db.commit()
    other = new_project(db, "同名")
    restore_project(db, project)
    db.commit()
    assert other.directory_key == "reader/同名"
    assert project.directory_key == "reader/同名（恢复）"
    assert (tmp_path / project.directory_key).is_dir()


def test_active_task_blocks_rename(db):
    project = new_project(db)
    db.add(Task(owner_id=project.owner_id, project_id=project.id, task_type="book.analyze", status="queued", payload={}))
    db.commit()
    with pytest.raises(ValueError, match="未完成任务"):
        rename_project(db, project, "新名字")
    assert project.name == "我的有声书"


def test_interrupted_move_is_recovered_even_when_directory_key_is_unchanged(db, tmp_path):
    project = new_project(db)
    source = tmp_path / project.directory_key / "01_input" / "source.txt"
    source.write_text("keep")
    target = source.with_name("moved.txt")
    source.rename(target)
    journal = tmp_path / ".layout-migrations" / f"{project.id}.json"
    journal.parent.mkdir()
    journal.write_text(json.dumps({"project_id": project.id, "target_key": project.directory_key,
                                  "token": "uncommitted", "actions": [{"op": "move", "source": str(source), "target": str(target)}]}))
    recover_layout_moves(db)
    assert source.read_text() == "keep"
    assert not target.exists()


def test_recovery_reads_append_journal_and_ignores_incomplete_last_action(db, tmp_path):
    project = new_project(db)
    source = tmp_path / project.directory_key / "01_input" / "source.txt"
    source.write_text("keep")
    target = source.with_name("moved.txt")
    source.rename(target)
    journal = tmp_path / ".layout-migrations" / f"{project.id}.json"
    journal.parent.mkdir()
    header = {"project_id": project.id, "target_key": project.directory_key, "token": "uncommitted", "actions": []}
    action = {"op": "move", "source": str(source), "target": str(target)}
    journal.write_text(json.dumps(header) + "\n" + json.dumps(action) + '\n{"op": "move"')
    recover_layout_moves(db)
    assert source.read_text() == "keep"
    assert not target.exists()


def test_previous_deployment_absolute_paths_are_updated_without_changing_ids(db, tmp_path):
    project = new_project(db)
    path = tmp_path / project.directory_key / "01_input" / "book (2).txt"
    path.write_text("source")
    file = ProjectFile(project_id=project.id, owner_id=project.owner_id, original_name="book.txt",
                       object_key=f"{project.directory_key}/01_input/{path.name}", sha256="a" * 64)
    db.add(file)
    db.flush()
    legacy_path = f"/previous/deployment/reader/{project.id}/01_input/{file.id}/book.txt"
    task = Task(project_id=project.id, owner_id=project.owner_id, status="succeeded", task_type="book.analyze", payload={})
    db.add(task)
    db.flush()
    result = TaskResult(task_id=task.id, result={"source_path": legacy_path})
    db.add(result)
    analysis = path.parent.parent / "03_parsed_json" / "analysis.json"
    analysis.write_text(json.dumps({"source": legacy_path}))
    db.commit()
    relocate_project(db, project, project.directory_key)
    db.commit()
    assert result.result["source_path"] == str(path)
    assert json.loads(analysis.read_text())["source"] == str(path)


def test_uncatalogued_files_keep_original_names_outside_temporary_cleanup(db, tmp_path):
    project = new_project(db)
    root = tmp_path / project.directory_key
    (root / "notes.txt").write_text("keep notes")
    folder = root / "11111111-1111-1111-1111-111111111111"
    folder.mkdir()
    (folder / "audio.wav").write_bytes(b"keep audio")
    relocate_project(db, project, project.directory_key, normalize=True)
    db.commit()
    assert (root / "07_output" / "历史文件" / "notes.txt").read_text() == "keep notes"
    assert (root / "07_output" / "历史文件" / "历史目录" / "audio.wav").read_bytes() == b"keep audio"
    assert set(entry.name for entry in root.iterdir()) == set(PROJECT_DIRECTORIES)


def test_offline_migration_handles_multiple_trashed_names_and_is_repeatable(db, tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from backend.platform.models import utcnow
    from backend.services import workspace_migration
    user = db.scalar(select(User))
    ids = []
    for key in ("reader/first-id", "reader/second-id"):
        project = Project(owner_id=user.id, name="同名旧书", directory_key=key, deleted_at=utcnow())
        db.add(project)
        db.flush()
        ids.append(project.id)
        (tmp_path / key).mkdir(parents=True)
    db.commit()
    monkeypatch.setattr(workspace_migration, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(workspace_migration, "configured_storage_root", lambda _db=None: tmp_path)
    assert workspace_migration.migrate_workspaces() == 2
    db.expire_all()
    assert {db.get(Project, id).directory_key for id in ids} == {
        "reader/同名旧书（回收站 1）", "reader/同名旧书（回收站 2）",
    }
    assert workspace_migration.migrate_workspaces() == 0


@pytest.mark.parametrize("name", ["a" * 176 + ".txt", "a" * 180, "a" * 169 + " .json"])
def test_long_collision_names_preserve_suffix_and_object_key(db, tmp_path, name):
    project = new_project(db)
    directory = tmp_path / project.directory_key / "01_input"
    original = storage.available_file_name(directory, name)
    (directory / original).write_text("original")
    second = storage.available_file_name(directory, name)
    assert second != original
    assert len(second) <= 180
    assert storage.safe_display_name(second) == second
    (directory / second).write_text("second")
    input_key = storage.project_input_object_key("reader", project.id, "file", second, db=db)
    output_key = storage.project_object_key("reader", project.id, "file", second, db=db, task_type="text.format")
    assert Path(input_key).name == second
    assert Path(output_key).name == second
    assert (tmp_path / input_key).read_text() == "second"
    if Path(original).suffix:
        assert Path(second).suffix == Path(original).suffix
    reserved = {original, second}
    fourth = storage.available_file_name(directory, name, reserved=reserved | {
        storage.available_file_name(directory, name, reserved=reserved)
    })
    assert fourth not in reserved and len(fourth) <= 180


def test_sanitization_is_idempotent_when_truncation_ends_with_dot_or_space():
    for character in (".", " "):
        name = "a" * 179 + character + "tail.txt"
        sanitized = storage.safe_display_name(name)
        assert storage.safe_display_name(sanitized) == sanitized


def test_restore_skips_casefold_conflicts_in_original_and_suffix_names(db, tmp_path):
    project = new_project(db, "Book")
    move_project_to_trash(db, project)
    db.commit()
    new_project(db, "book")
    new_project(db, "book（恢复）")
    restore_project(db, project)
    db.commit()
    assert project.name == "Book（恢复 2）"
    assert project.directory_key == "reader/Book（恢复 2）"
    assert (tmp_path / project.directory_key).is_dir()


def test_restore_skips_uncatalogued_directory_without_overwriting(db, tmp_path):
    project = new_project(db, "Book")
    move_project_to_trash(db, project)
    db.commit()
    occupied = tmp_path / "reader" / "book"
    occupied.mkdir()
    (occupied / "notes.txt").write_text("preserve")
    restore_project(db, project)
    db.commit()
    assert project.name == "Book（恢复）"
    assert (occupied / "notes.txt").read_text() == "preserve"


def test_workspace_storage_syntax_with_python310_when_available():
    import shutil
    import subprocess
    import sys
    interpreter = sys.executable if sys.version_info[:2] == (3, 10) else shutil.which("python3.10")
    if not interpreter:
        pytest.skip("Python 3.10 interpreter is not installed")
    sources = [Path(storage.__file__), Path(workspace_layout.__file__),
               Path(__file__).resolve().parents[1] / "services" / "projects.py"]
    result = subprocess.run([
        interpreter, "-c", "import sys; [compile(open(p, encoding='utf-8').read(), p, 'exec') for p in sys.argv[1:]]",
        *map(str, sources),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("task_type", "module"), [
    ("book.analyze", "00_temp"),
    ("book.split", "02_split_text"),
    ("script.parse", "03_parsed_json"),
])
def test_text_artifact_modules_keep_analysis_out_of_scripts(task_type, module):
    assert storage.artifact_module(task_type, "book_analysis.json") == module
