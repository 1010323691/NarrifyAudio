"""Real submitted file/state versions invalidate work without full-project reads."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.input_versions import bind_metadata, capture_files, reset_metadata, validate_files, validating_inputs, check_bound_inputs
from backend.core.request_context import bind_workspace, reset_workspace
from backend.core.script_snapshot import bind_reference, capture_reference, reset_reference
from backend.platform.task_contracts import TaskExecutionError
from backend.platform.task_input_validation import validate_task_inputs
from backend.api.bgm import _versioned_payload
from backend.core.paths import Layout
from backend.engines import bgm, voices


def test_shared_role_reference_bypasses_dynamic_selection_and_rejects_source_changes(tmp_path, monkeypatch):
    path = tmp_path / "03_parsed_json" / "chapter.json"
    path.parent.mkdir()
    path.write_text(json.dumps([{"speaker": "A", "text": "one"}, {"speaker": "B", "text": "two"}]), encoding="utf-8")
    reference = capture_reference([path], tmp_path, "__all__")
    workspace_token, reference_token = bind_workspace(tmp_path), bind_reference(reference)
    metadata_token = bind_metadata({"_script_inputs": reference})
    monkeypatch.setattr(voices, "resolve_parsed_json_all", lambda: pytest.fail("batch roles must not reselect whole-book paths"))
    handle = SimpleNamespace(check=lambda: None, log=lambda *args: None, cancelled=False)
    try:
        first = voices._load_script(handle, "__all__", ["A"])
        second = voices._load_script(handle, "__all__", ["B"])
        assert first.path == second.path
        assert first.counts == {"A": 1} and second.counts == {"B": 1}
        assert len(list(first.path.parent.glob("*.sqlite"))) == 1
        validate_task_inputs({})
        path.write_text(json.dumps([{"speaker": "C", "text": "changed"}]), encoding="utf-8")
        with pytest.raises(RuntimeError, match="已变更"):
            voices._load_script(handle, "__all__", ["A"])
        with pytest.raises(TaskExecutionError) as error:
            validate_task_inputs({})
        assert error.value.code == "input_changed"
    finally:
        reset_metadata(metadata_token); reset_reference(reference_token); reset_workspace(workspace_token)


def test_optional_input_appearance_and_replacement_are_versions(tmp_path):
    path = tmp_path / "source.wav"
    path.write_bytes(b"original")
    records = capture_files(tmp_path, ["source.wav", "source.mp3"])
    validate_files(tmp_path, records)
    (tmp_path / "source.mp3").write_bytes(b"new precedence")
    with pytest.raises(RuntimeError):
        validate_files(tmp_path, records)
    (tmp_path / "source.mp3").unlink()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replaced")
    replacement.replace(path)
    with pytest.raises(RuntimeError):
        validate_files(tmp_path, records)


def test_bgm_chapter_binding_survives_other_chapter_edits_and_storage_migration(tmp_path):
    layout = Layout(tmp_path)
    layout.split_text.mkdir(parents=True)
    (layout.split_text / "one.txt").write_text("chapter", encoding="utf-8")
    assignment = {"music": None, "locked": False, "segment": False}
    # Capture legacy logical content; a representation-only migration must not
    # invalidate the submitted chapter version.
    layout.bgm.mkdir()
    (layout.bgm / "bgm_assignments.json").write_text(json.dumps({"chapters": {"one": assignment, "two": {"music": None}}}), encoding="utf-8")
    payload = _versioned_payload(layout, "one", {"stem": "one"}, assignment=assignment)
    workspace_token = bind_workspace(tmp_path)
    try:
        validate_task_inputs(payload)
        bgm.update_assignments(layout, lambda data: data["chapters"]["two"].update(locked=True), stems=["two"])
        validate_task_inputs(payload)
        bgm.update_assignments(layout, lambda data: data["chapters"]["one"].update(locked=True), stems=["one"])
        with pytest.raises(TaskExecutionError) as error:
            validate_task_inputs(payload)
        assert error.value.code == "input_changed"
    finally:
        reset_workspace(workspace_token)


def test_execution_checkpoint_revalidates_after_wait_and_resets_binding(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"accepted")
    records = capture_files(tmp_path, ["input"])
    with pytest.raises(RuntimeError):
        with validating_inputs(lambda: validate_files(tmp_path, records)):
            path.write_bytes(b"changed while waiting")
            check_bound_inputs()
    check_bound_inputs()  # no previous project's callback leaks to the next task


def test_music_library_version_is_shared_by_batch_and_checked_on_execution(tmp_path, monkeypatch):
    from backend.engines import music
    index = tmp_path / "music_index.json"
    index.write_text('{"tracks":{}}', encoding="utf-8")
    metadata_token = bind_metadata({"_music_inputs": capture_files(tmp_path, ["music_index.json"])})
    workspace_token = bind_workspace(tmp_path)
    monkeypatch.setattr(music, "_library_dir", lambda: tmp_path)
    try:
        validate_task_inputs({})
        index.write_text('{"tracks":{"new":{}}}', encoding="utf-8")
        with pytest.raises(TaskExecutionError) as error:
            validate_task_inputs({})
        assert error.value.code == "input_changed"
    finally:
        reset_metadata(metadata_token); reset_workspace(workspace_token)


@pytest.mark.parametrize("segment", [False, True])
def test_mix_acceptance_binds_actual_music_even_if_index_is_unchanged(tmp_path, monkeypatch, segment):
    from backend.engines import music
    layout = Layout(tmp_path / "workspace")
    library = tmp_path / "library"
    library.mkdir()
    track = library / "track.wav"
    track.write_bytes(b"original")
    monkeypatch.setattr(music, "_library_dir", lambda: library)
    assignment = {"music": None if segment else "track.wav", "segment": segment}
    if segment:
        timeline = bgm._timeline_path(layout, "one")
        timeline.parent.mkdir(parents=True)
        timeline.write_text(json.dumps({"timeline": [{"music_id": "track.wav"}]}), encoding="utf-8")
    payload = _versioned_payload(layout, "one", {"stem": "one"}, assignment=assignment)
    # Exercise the captured audio identities independently of chapter storage.
    payload.pop("_assignment_version")
    workspace_token = bind_workspace(layout.workspace)
    try:
        validate_task_inputs(payload)
        track.write_bytes(b"new audio without an index edit")
        with pytest.raises(TaskExecutionError) as error:
            validate_task_inputs(payload)
        assert error.value.code == "input_changed"
    finally:
        reset_workspace(workspace_token)
