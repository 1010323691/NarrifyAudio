"""Batch merge / undo / veto endpoints over a throwaway workspace."""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import backend.api.tts as tts_api
from backend.api.tts import (
    LinkVetoRequest, MergeBatchRequest, MergeUndoRequest, ReviewSeenRequest, list_voices, link_veto,
    merge_batch, merge_graph, merge_review_seen, merge_undo, unlink_veto,
)
from backend.core import role_links as RL
from backend.core.paths import get_or_prepare_layout
from backend.engines import role_merge as RM
from backend.tests.test_voices import _load_vc, _quiet_durable_task_guard, _seed_foundations, _seed_script, clone_ws  # noqa: F401

NAMES = {"林黛玉": 5, "黛玉": 3, "黛玉儿": 2}


@pytest.fixture
def ws(clone_ws):
    _seed_script(clone_ws, NAMES)
    _seed_foundations(clone_ws, list(NAMES))
    return clone_ws


def _speakers(ws):
    return [e["speaker"] for e in json.loads((ws / "03_parsed_json" / "s.json").read_text("utf-8"))]


def test_list_reads_link_table_with_basis(ws):
    rows = {r["name"]: r for r in list_voices(script="__all__")["speakers"]}
    assert rows["黛玉"]["alias_of"] == "林黛玉" and rows["黛玉"]["alias_basis"] == "名字包含"
    assert rows["黛玉儿"]["alias_of"] == "黛玉"
    # reads (list and merge-graph) never persist: the table appears with the first write
    merge_graph(script="__all__")
    assert not (ws / "04_voice_profiles" / "role_links").exists()
    merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉儿"]))
    assert (ws / "04_voice_profiles" / "role_links" / "all.json").is_file()


def test_scopes_do_not_share_state_files(ws):
    layout = get_or_prepare_layout()
    assert RM._state_path(layout, None) == RM._state_path(layout, "s.json")  # newest file, not the whole book
    assert RM._state_path(layout, None) != RM._state_path(layout, "__all__")
    version = merge_graph(script="__all__")["version"]
    list_voices(script=None)
    list_voices(script="s.json")
    assert merge_graph(script="__all__")["version"] == version


def test_batch_merge_rewrites_orphans_and_records(ws):
    graph = merge_graph(script="__all__")
    out = merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉"], version=graph["version"]))
    assert out["replaced"] == 3 and out["orphans"] == ["黛玉儿"]
    assert out["rematched"] == {"黛玉儿": "林黛玉"}
    assert sorted(set(_speakers(ws))) == ["林黛玉", "黛玉儿"]
    assert "黛玉" not in _load_vc(ws)
    new_graph = out["graph"]
    assert new_graph["new_candidates"] == {"林黛玉": ["黛玉儿"]}
    assert new_graph["links"]["黛玉儿"]["target"] == "林黛玉"
    record = new_graph["records"][0]
    assert (record["source"], record["line_count"], record["undoable"]) == ("黛玉", 3, True)
    assert new_graph["version"] > graph["version"]


def test_version_conflict_and_active_task_are_409(ws, monkeypatch):
    version = merge_graph(script="__all__")["version"]
    with pytest.raises(HTTPException) as err:
        merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉"], version=version + 5))
    assert err.value.status_code == 409
    assert "黛玉" in set(_speakers(ws))
    monkeypatch.setattr(tts_api, "_phase_task_active", lambda *a: True)
    with pytest.raises(HTTPException) as err:
        merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉"]))
    assert err.value.status_code == 409


def test_failed_commit_leaves_files_and_state_untouched(ws, monkeypatch):
    before_script = (ws / "03_parsed_json" / "s.json").read_text("utf-8")
    before_vc = (ws / "04_voice_profiles" / "voice_config.json").read_text("utf-8")
    merge_graph(script="__all__")
    monkeypatch.setattr(RL, "save_state", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉", "黛玉儿"]))
    assert json.loads((ws / "03_parsed_json" / "s.json").read_text("utf-8")) == json.loads(before_script)
    assert json.loads((ws / "04_voice_profiles" / "voice_config.json").read_text("utf-8")) == json.loads(before_vc)


def test_invalid_requests(ws):
    for target, sources, status in [("林黛玉", ["林黛玉"], 400), ("林黛玉", ["不存在"], 404), ("不存在", ["黛玉"], 400)]:
        with pytest.raises(HTTPException) as err:
            merge_batch(MergeBatchRequest(script="__all__", target=target, sources=sources))
        assert err.value.status_code == status


def test_undo_restores_lines_voice_config_and_link(ws):
    original = _speakers(ws)
    vc = _load_vc(ws)
    out = merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉"]))
    rid = out["graph"]["records"][0]["id"]
    undone = merge_undo(MergeUndoRequest(script="__all__", record_id=rid))
    assert _speakers(ws) == original
    assert _load_vc(ws)["黛玉"] == vc["黛玉"]
    assert undone["graph"]["records"] == []
    assert undone["graph"]["links"]["黛玉"]["target"] == "林黛玉"
    # the rematch of 黛玉儿 is not rolled back
    assert undone["graph"]["links"]["黛玉儿"]["target"] == "林黛玉"


def test_undo_refused_after_lines_changed(ws):
    out = merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉"]))
    path = ws / "03_parsed_json" / "s.json"
    data = json.loads(path.read_text("utf-8"))
    next(e for e in data if e["text"].startswith("黛玉 speaks"))["text"] = "edited"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    graph = merge_graph(script="__all__")
    assert graph["records"][0]["undoable"] is False and graph["records"][0]["reason"]
    with pytest.raises(HTTPException) as err:
        merge_undo(MergeUndoRequest(script="__all__", record_id=out["graph"]["records"][0]["id"]))
    assert err.value.status_code == 409


def test_veto_and_restore_and_review_seen(ws):
    merge_graph(script="__all__")
    out = link_veto(LinkVetoRequest(script="__all__", source="黛玉儿", target="黛玉"))
    assert out["graph"]["links"]["黛玉儿"]["target"] == "林黛玉"  # rematched elsewhere
    assert out["graph"]["new_candidates"]["林黛玉"] == ["黛玉儿"]
    assert merge_review_seen(ReviewSeenRequest(script="__all__", target="林黛玉"))["cleared"] is True
    back = unlink_veto(source="黛玉儿", target="黛玉", script="__all__", version=None)
    assert back["restored"] is False and back["graph"]["vetoes"] == []
    with pytest.raises(HTTPException) as err:
        link_veto(LinkVetoRequest(script="__all__", source="黛玉儿", target="黛玉"))
    assert err.value.status_code == 404  # that link no longer exists


def test_voice_config_only_role_merge_and_undo_keep_version_stable(ws):
    _seed_foundations(ws, [*NAMES, "幽灵"])  # 幽灵 has a voice entry but no lines
    out = merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["幽灵"]))
    version = out["graph"]["version"]
    assert merge_graph(script="__all__")["version"] == version
    undone = merge_undo(MergeUndoRequest(script="__all__", record_id=out["graph"]["records"][0]["id"]))
    assert "幽灵" in _load_vc(ws)
    assert merge_graph(script="__all__")["version"] == undone["graph"]["version"]


def test_failed_rollback_is_reported_not_swallowed(ws, monkeypatch, caplog):
    real = RM.pathio.rewrite_json_file
    state = {"writes": 0}

    def flaky(path, data):
        state["writes"] += 1
        if state["writes"] > 1:  # first write lands; the rollback write fails
            raise OSError("disk gone")
        return real(path, data)

    link_veto(LinkVetoRequest(script="__all__", source="黛玉儿", target="黛玉"))  # persists the table first
    monkeypatch.setattr(RL, "save_state", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(RM.pathio, "rewrite_json_file", flaky)
    with pytest.raises(HTTPException) as err:
        merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉"]))
    assert err.value.status_code == 500 and "s.json" in err.value.detail
    assert "rollback failed" in caplog.text


def test_task_started_after_the_request_guard_is_caught_inside_the_lock(ws, monkeypatch):
    calls = iter([False, True])  # request guard passes; re-check under the lock sees a task
    monkeypatch.setattr(tts_api, "_phase_task_active", lambda *a: next(calls))
    before = _speakers(ws)
    with pytest.raises(HTTPException) as err:
        merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉"]), ctx=object(), db=object())
    assert err.value.status_code == 409
    assert _speakers(ws) == before


def test_a_write_that_fails_midway_is_restored_too(ws, monkeypatch):
    path = ws / "03_parsed_json" / "s.json"
    before = path.read_text("utf-8")
    real = RM.pathio.rewrite_json_file

    def truncating(target, data):
        if target == path and data is not None and any(e.get("speaker") == "林黛玉" and e["text"].startswith("黛玉 ") for e in data):
            Path(target).write_text("{truncated")  # the write dies after clobbering the file
            raise OSError("disk full")
        return real(target, data)

    monkeypatch.setattr(RM.pathio, "rewrite_json_file", truncating)
    with pytest.raises(OSError):
        merge_batch(MergeBatchRequest(script="__all__", target="林黛玉", sources=["黛玉"]))
    assert json.loads(path.read_text("utf-8")) == json.loads(before)
