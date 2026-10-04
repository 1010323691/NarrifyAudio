"""Tests for workspace-relative path serialization / resolution (``core/pathio.py``).

Pins the location-independence contract of the persisted artifact JSONs:

* paths to files INSIDE the workspace are stored relative to the workspace root
  (forward-slash form) and resolved against the *current* root at read time;
* global / external resources (e.g. the ffmpeg binary, the workspace pointer)
  stay absolute and are never converted;
* legacy absolute values (written before this design) are migrated to the
  relative form — on save, and lazily on first load — idempotently;
* a whole project can be moved to another location: after re-selecting the
  (new) workspace folder, every in-project reference still resolves, and
  unresolvable values raise a clear error instead of failing silently.

The big end-to-end test (create → save JSON → move the whole workspace tree →
re-point → reload) exercises the real engine / API functions against the moved
project.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from backend.core import pathio
from backend.core import config as core_config
from backend.core import paths as core_paths

from fastapi import HTTPException


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Redirect the project-root globals into ``tmp_path`` (clean root setting.json)."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(
        json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8"
    )
    core_config.reset_config_cache()
    yield tmp_path
    core_config.reset_config_cache()


def _set_pointer(sandbox, ws):
    core_config.set_workspace_pointer(str(ws))


# --------------------------------------------------------------------------- #
# to_workspace_relative — the write side
# --------------------------------------------------------------------------- #

def test_to_rel_value_inside_workspace(tmp_path):
    ws = tmp_path / "ws"
    v = ws / "05_audio_chunk" / "s" / "0001.mp3"
    assert pathio.to_workspace_relative(str(v), ws) == "05_audio_chunk/s/0001.mp3"


def test_to_rel_backslash_style_value(tmp_path):
    # A value written with Windows backslashes still serializes to the slash form.
    ws = tmp_path / "ws"
    v = str(ws).replace(os.sep, "/") + "/04_voice_profiles/designed_voices/a.wav"
    assert pathio.to_workspace_relative(v, ws) == "04_voice_profiles/designed_voices/a.wav"


def test_to_rel_value_equal_to_root_is_empty(tmp_path):
    ws = tmp_path / "ws"
    assert pathio.to_workspace_relative(str(ws), ws) == ""


@pytest.mark.skipif(os.name != "nt", reason="case-insensitivity is a Windows property")
def test_to_rel_is_case_insensitive_on_windows(tmp_path):
    ws = tmp_path / "ws"
    v = str(ws / "01_input" / "A.TXT").upper()  # "C:\\...\\WS\\01_INPUT\\A.TXT"
    assert pathio.to_workspace_relative(v, ws) == "01_input/a.txt"


def test_to_rel_value_outside_workspace_is_none(tmp_path):
    ws = tmp_path / "ws"
    other = tmp_path / "elsewhere" / "ffmpeg.exe"
    assert pathio.to_workspace_relative(str(other), ws) is None  # external → stays absolute


def test_to_rel_empty_and_none_values(tmp_path):
    ws = tmp_path / "ws"
    assert pathio.to_workspace_relative("", ws) is None
    assert pathio.to_workspace_relative("   ", ws) is None
    assert pathio.to_workspace_relative(None, ws) is None
    assert pathio.to_workspace_relative("x", None) is None


def test_to_rel_relative_input_normalized(tmp_path):
    ws = tmp_path / "ws"
    assert pathio.to_workspace_relative("05_audio_chunk/./s//0001.mp3", ws) == "05_audio_chunk/s/0001.mp3"
    assert pathio.to_workspace_relative("a\\b", ws) == "a/b"


def test_to_rel_relative_escape_raises(tmp_path):
    ws = tmp_path / "ws"
    with pytest.raises(pathio.PathOutsideWorkspace):
        pathio.to_workspace_relative("../evil.txt", ws)
    with pytest.raises(pathio.PathOutsideWorkspace):
        pathio.to_workspace_relative("a/../../evil.txt", ws)
    # .. that stays inside is fine
    assert pathio.to_workspace_relative("a/b/../c.txt", ws) == "a/c.txt"


# --------------------------------------------------------------------------- #
# resolve_path — the read side
# --------------------------------------------------------------------------- #

def _legacy_project(tmp_path, parent, ws_name="重活了"):
    """A legacy-format project under ``tmp_path/<parent>/``: artifact JSONs holding
    ABSOLUTE paths (pre-upgrade). ``parent`` is the location that will be moved away."""
    ws = tmp_path / parent / ws_name
    (ws / "05_audio_chunk" / "s").mkdir(parents=True)
    (ws / "04_voice_profiles" / "designed_voices").mkdir(parents=True)
    (ws / "01_input").mkdir(parents=True)
    (ws / "05_audio_chunk" / "s" / "0001.mp3").write_bytes(b"0" * 16)
    (ws / "05_audio_chunk" / "s" / "0002.mp3").write_bytes(b"0" * 16)
    (ws / "04_voice_profiles" / "designed_voices" / "sp.wav").write_bytes(b"0" * 16)
    (ws / "01_input" / "book.txt").write_text("正文", encoding="utf-8")
    (ws / "05_audio_chunk" / "s" / "manifest.json").write_text(json.dumps([
        {"index": 0, "speaker": "A", "text": "t0", "pause_after": None,
         "path": str(ws / "05_audio_chunk" / "s" / "0001.mp3"), "ok": True, "reason": ""},
        {"index": 1, "speaker": "B", "text": "t1", "pause_after": 900,
         "path": str(ws / "05_audio_chunk" / "s" / "0002.mp3"), "ok": True, "reason": ""},
    ], ensure_ascii=False), encoding="utf-8")
    (ws / "04_voice_profiles" / "voice_config.json").write_text(json.dumps({
        "A": {"type": "clone", "description": "d", "ref_text": "r",
              "ref_audio": str(ws / "04_voice_profiles" / "designed_voices" / "sp.wav")},
    }, ensure_ascii=False), encoding="utf-8")
    from backend.engines.tts_batch import voice_params, voice_signature
    voice_config = json.loads(
        (ws / "04_voice_profiles" / "voice_config.json").read_text("utf-8")
    )
    manifest_path = ws / "05_audio_chunk" / "s" / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    for entry in manifest:
        entry["voice_signature"] = voice_signature(entry["speaker"], voice_config)
        entry["voice_used"] = voice_params(entry["speaker"], voice_config)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return ws


def test_resolve_relative_against_current_root(tmp_path):
    ws = tmp_path / "ws"
    p = pathio.resolve_path("05_audio_chunk/s/0001.mp3", ws)
    assert p == ws / "05_audio_chunk" / "s" / "0001.mp3"


def test_resolve_legacy_absolute_inside(tmp_path):
    ws = tmp_path / "ws"
    v = str(ws / "01_input" / "book.txt")
    assert pathio.resolve_path(v, ws) == Path(v)  # a legacy value inside the root is valid as-is


def test_resolve_external_existing_stays_absolute(tmp_path):
    ws = tmp_path / "ws"
    (tmp_path / "tools").mkdir()
    ext = tmp_path / "tools" / "ffmpeg.exe"
    ext.write_bytes(b"x")  # the tool binary is a real file OUTSIDE the workspace
    assert pathio.resolve_path(str(ext), ws) == ext  # a global resource, untouched


def test_resolve_empty_and_none(tmp_path):
    ws = tmp_path / "ws"
    assert pathio.resolve_path("", ws) is None
    assert pathio.resolve_path(None, ws) is None
    assert pathio.resolve_path("x", None) is None


def test_resolve_relative_escape_raises(tmp_path):
    ws = tmp_path / "ws"
    with pytest.raises(pathio.PathOutsideWorkspace):
        pathio.resolve_path("../../etc/passwd", ws)


def test_resolve_stale_recovered_by_structure(tmp_path):
    """The workspace moved: the old absolute path's tail after the known workspace
    directory re-anchors under the new root (requirement: recover by original structure)."""
    ws_old = tmp_path / "OldPlace" / "重活了"
    _legacy_project(tmp_path, "OldPlace", "重活了")  # builds tmp_path/OldPlace/重活了
    ws_new = tmp_path / "NewPlace" / "重活了"
    ws_new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ws_old), str(ws_new))
    stale = str(ws_old / "05_audio_chunk" / "s" / "0001.mp3")
    p = pathio.resolve_path(stale, ws_new)
    assert p == ws_new / "05_audio_chunk" / "s" / "0001.mp3"
    assert p.exists()


def test_resolve_stale_recovered_by_name(tmp_path):
    """No workspace-directory marker in the old path → a unique file name still recovers it."""
    ws_old = tmp_path / "OldPlace" / "misc"
    ws_old.mkdir(parents=True)
    (ws_old / "unique_tool.bin").write_bytes(b"x")
    ws_new = tmp_path / "NewPlace" / "misc"
    ws_new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ws_old), str(ws_new))
    stale = str(ws_old / "unique_tool.bin")
    p = pathio.resolve_path(stale, ws_new)
    assert p == ws_new / "unique_tool.bin"
    assert p.exists()


def test_resolve_stale_ambiguous_name_fails_clearly(tmp_path):
    ws_old = tmp_path / "OldPlace" / "misc"
    (ws_old / "a" / "dup.bin").parent.mkdir(parents=True)
    (ws_old / "a" / "dup.bin").write_bytes(b"x")
    (ws_old / "b" / "dup.bin").parent.mkdir(parents=True)
    (ws_old / "b" / "dup.bin").write_bytes(b"y")
    ws_new = tmp_path / "NewPlace" / "misc"
    ws_new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ws_old), str(ws_new))
    stale = str(ws_old / "a" / "dup.bin")
    with pytest.raises(pathio.PathNotFoundError):
        pathio.resolve_path(stale, ws_new, strict=True)
    assert pathio.resolve_path(stale, ws_new, strict=False) is None


def test_resolve_missing_inside_workspace_is_plain_missing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # A relative value that simply does not exist: strict and soft both yield the path;
    # the caller's existence check reports the (clear) miss — no false recovery.
    p = pathio.resolve_path("01_input/nope.txt", ws, strict=True)
    assert p == ws / "01_input" / "nope.txt"


def test_resolve_missing_legacy_inside(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    v = str(ws / "05_audio_chunk" / "gone.mp3")
    p = pathio.resolve_path(v, ws, strict=True)  # inside the current root: no recovery needed
    assert p == Path(v)


# --------------------------------------------------------------------------- #
# legacy migration (idempotent, in place)
# --------------------------------------------------------------------------- #

def test_migrate_entries_converts_only_in_workspace_values(tmp_path):
    ws = tmp_path / "ws"
    ext = tmp_path / "elsewhere" / "tool.exe"
    entries = [
        {"path": str(ws / "05_audio_chunk" / "a.mp3")},
        {"path": str(ext)},          # external → untouched
        {"path": ""},                # empty → untouched
        {"path": "04_voice_profiles/x.wav"},  # already relative → unchanged value
    ]
    n = pathio.migrate_entries(entries, ws, ("path",))
    assert n == 1
    assert entries[0]["path"] == "05_audio_chunk/a.mp3"
    assert entries[1]["path"] == str(ext)
    assert entries[2]["path"] == ""
    assert entries[3]["path"] == "04_voice_profiles/x.wav"
    # idempotent: a second pass changes nothing
    assert pathio.migrate_entries(entries, ws, ("path",)) == 0


def test_migrate_entries_in_rewrites_file(tmp_path):
    ws = tmp_path / "ws"
    (ws / "05_audio_chunk" / "s").mkdir(parents=True)
    (ws / "05_audio_chunk" / "s" / "0001.mp3").write_bytes(b"0")
    f = ws / "05_audio_chunk" / "s" / "manifest.json"
    f.write_text(json.dumps([
        {"index": 0, "path": str(ws / "05_audio_chunk" / "s" / "0001.mp3"), "ok": True},
    ]), encoding="utf-8")
    n, data = pathio.migrate_entries_in(f, ws, "list", ("path",))
    assert n == 1
    assert data[0]["path"] == "05_audio_chunk/s/0001.mp3"  # the parsed copy is migrated in place
    on_disk = json.loads(f.read_text("utf-8"))
    assert on_disk[0]["path"] == "05_audio_chunk/s/0001.mp3"  # …and the file rewritten
    n2, _ = pathio.migrate_entries_in(f, ws, "list", ("path",))
    assert n2 == 0  # idempotent


def test_migrate_entries_in_dict_kind(tmp_path):
    ws = tmp_path / "ws"
    (ws / "04_voice_profiles" / "designed_voices").mkdir(parents=True)
    (ws / "04_voice_profiles" / "designed_voices" / "x.wav").write_bytes(b"0")
    f = ws / "04_voice_profiles" / "voice_config.json"
    f.write_text(json.dumps({
        "A": {"type": "clone", "ref_audio": str(ws / "04_voice_profiles" / "designed_voices" / "x.wav")},
    }), encoding="utf-8")
    n, data = pathio.migrate_entries_in(f, ws, "dict", ("ref_audio",))
    assert n == 1
    assert data["A"]["ref_audio"] == "04_voice_profiles/designed_voices/x.wav"


def test_migrate_entries_in_voice_config_candidates_untouched(tmp_path):
    # voice_config entries may also carry a candidates list (per-character clone
    # candidates): the migration must touch only each entry's top-level ref_audio and
    # leave the nested candidates' already-relative paths byte-for-byte intact.
    ws = tmp_path / "ws"
    dv = ws / "04_voice_profiles" / "designed_voices"
    dv.mkdir(parents=True)
    for name in ("x.wav", "y.wav"):
        (dv / name).write_bytes(b"0")
    f = ws / "04_voice_profiles" / "voice_config.json"
    f.write_text(json.dumps({
        "A": {
            "type": "clone",
            "ref_audio": str(ws / dv / "x.wav"),  # legacy absolute -> migrated
            "candidates": [
                {"id": "1", "ref_audio": "04_voice_profiles/designed_voices/x.wav", "seed": 1},
                {"id": "2", "ref_audio": "04_voice_profiles/designed_voices/y.wav", "seed": 2},
            ],
            "selected_audio_id": "1",
        },
    }), encoding="utf-8")
    n, data = pathio.migrate_entries_in(f, ws, "dict", ("ref_audio",))
    assert n == 1
    assert data["A"]["ref_audio"] == "04_voice_profiles/designed_voices/x.wav"
    assert [c["ref_audio"] for c in data["A"]["candidates"]] == [
        "04_voice_profiles/designed_voices/x.wav",
        "04_voice_profiles/designed_voices/y.wav",
    ]
    assert data["A"]["selected_audio_id"] == "1"
    # idempotent: a second pass migrates nothing new
    n2, _ = pathio.migrate_entries_in(f, ws, "dict", ("ref_audio",))
    assert n2 == 0


def test_migrate_entries_in_missing_file_is_inert(tmp_path):
    n, data = pathio.migrate_entries_in(tmp_path / "nope.json", tmp_path / "ws", "list")
    assert n == 0 and data is None


# --------------------------------------------------------------------------- #
# the end-to-end move scenario (create → save → move → reload)
# --------------------------------------------------------------------------- #

def test_move_project_manifest_and_voice_config(sandbox):
    """Create a legacy-format project, move the WHOLE workspace folder, re-point, and
    confirm every in-project reference still resolves — and the JSONs now hold
    workspace-relative paths."""
    import backend.engines.tts_batch as tts_batch
    import backend.engines.merge as merge
    from backend.api.tts import batch_status, list_voices

    ws = _legacy_project(sandbox, "OldPlace", "重活了")
    (ws / "03_parsed_json").mkdir()
    (ws / "03_parsed_json" / "s.json").write_text(json.dumps([
        {"speaker": "A", "text": "t0", "instruct": ""},
        {"speaker": "B", "text": "t1", "instruct": ""},
    ], ensure_ascii=False), encoding="utf-8")
    _set_pointer(sandbox, ws)

    ws_new = sandbox / "NewPlace" / "重活了"
    ws_new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ws), str(ws_new))
    _set_pointer(sandbox, ws_new)  # the user re-selects the moved folder

    # -- manifest: resolves, and is migrated to the relative form on load -----------
    out_dir = ws_new / "05_audio_chunk" / "s"
    m = tts_batch.load_manifest(out_dir)
    assert set(m) == {0, 1}
    assert all(tts_batch.is_done(e) for e in m.values())  # files found at the NEW location
    on_disk = json.loads((out_dir / "manifest.json").read_text("utf-8"))
    for e in on_disk:
        assert not os.path.isabs(e["path"]), "manifest must store workspace-relative paths"
        assert e["path"].startswith("05_audio_chunk/s/")
    # a second load changes nothing (idempotent migration)
    assert tts_batch.load_manifest(out_dir) == m

    # -- voice config: migrated, and the voices endpoint previews from the new root --
    v = list_voices("s.json")
    sp = {s["name"]: s for s in v["speakers"]}
    assert sp["A"]["status"] == "ready"
    assert sp["A"]["preview"] == "designed_voices/sp.wav"  # relative to 04_voice_profiles/
    vc = json.loads((ws_new / "04_voice_profiles" / "voice_config.json").read_text("utf-8"))
    assert not os.path.isabs(vc["A"]["ref_audio"])
    assert vc["A"]["ref_audio"] == "04_voice_profiles/designed_voices/sp.wav"

    # -- batch status counts the completed segments at the new location --------------
    assert batch_status("s.json") == {"total": 2, "completed": 2, "remaining": 0}

    # -- merge resolves every segment to a real file under the new root --------------
    manifest = json.loads((out_dir / "manifest.json").read_text("utf-8"))
    segs, missing = merge.collect_segments(manifest, core_paths.get_or_prepare_layout().workspace)
    assert missing == 0
    assert [s["index"] for s in segs] == [0, 1]
    for s in segs:  # the worker gets absolute paths (the transient file), under the NEW root
        assert os.path.isabs(s["path"])
        assert s["path"].startswith(str(ws_new) + os.sep)
        assert os.path.exists(s["path"])


def test_move_project_synthesize_resume(sandbox, monkeypatch):
    """A legacy project moved before a *new* synthesis run: resume must skip the
    already-done segments (their files live at the new location) and the run must
    store workspace-relative paths + hand the worker the workspace root."""
    from collections import deque
    from pathlib import Path as P

    import backend.engines.tts_batch as tts_batch
    from backend.engines.tts import WorkerWatchdogTimeout  # noqa: F401

    ws = _legacy_project(sandbox, "OldPlace", "重活了")
    (ws / "03_parsed_json").mkdir()
    (ws / "03_parsed_json" / "s.json").write_text(json.dumps([
        {"speaker": "A", "text": "t0", "instruct": ""},
        {"speaker": "B", "text": "t1", "instruct": ""},
    ], ensure_ascii=False), encoding="utf-8")
    # Only segment 0 is already done (its file survives the move); segment 1 is pending,
    # so the resume actually spawns the engine for exactly that one segment.
    (ws / "05_audio_chunk" / "s" / "0002.mp3").unlink()
    _set_pointer(sandbox, ws)
    ws_new = sandbox / "NewPlace" / "重活了"
    ws_new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ws), str(ws_new))
    _set_pointer(sandbox, ws_new)

    captured = {}

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:  # the real worker writes the file AND reports an ABSOLUTE path
            p = Path(os.path.join(out_dir, str(s["index"] + 1).zfill(4) + ".mp3"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"0" * 16)
            on_line(f"[segment] {s['index']} ok {p}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (P("/fake/python"), P("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)

    class _Handle:
        logs, progresses = [], []
        def log(self, msg, level="INFO"):
            self.logs.append((level, msg))
        def progress(self, frac, current=""):
            self.progresses.append((frac, current))
        def check(self):
            pass
        def segment_stats(self, done, total, chars_done, chars_total):
            pass  # display-only metric — this test pins the path migration, not the metric

    result = tts_batch.synthesize(_Handle(), None, "s.json", None)  # default = resume

    # index 0 was already done (its file is at the new location) → only index 1 ran
    seg_file = captured["cmd"][captured["cmd"].index("--segments-file") + 1]
    assert [s["index"] for s in json.load(open(seg_file, encoding="utf-8"))] == [1]
    assert result["completed"] == 1 and result["failed"] == []
    assert result["done_count"] == 2  # both count as done at the end

    # the worker was handed the (new) workspace root
    assert captured["cmd"][captured["cmd"].index("--workspace") + 1] == str(ws_new)

    # the manifest now holds relative paths for every entry
    on_disk = json.loads((ws_new / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))
    by_index = {e["index"]: e for e in on_disk}
    assert by_index[0]["path"] == "05_audio_chunk/s/0001.mp3"  # the legacy entry, migrated
    assert by_index[1]["path"] == "05_audio_chunk/s/0002.mp3"  # this run's absolute output, converted
    assert result["completed"] == 1


def test_move_project_inbound_paths_recovered(sandbox):
    """API endpoints accept a stale absolute path (sent by a UI that still holds the
    old location) and recover it against the new workspace; unresolvable values get
    a clear 400."""
    from backend.api import _common

    ws = _legacy_project(sandbox, "OldPlace", "重活了")
    _set_pointer(sandbox, ws)
    stale = str(ws / "01_input" / "book.txt")
    ws_new = sandbox / "NewPlace" / "重活了"
    ws_new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ws), str(ws_new))
    _set_pointer(sandbox, ws_new)

    p = _common.resolve_inbound_path(stale)
    assert p == ws_new / "01_input" / "book.txt"
    assert p.is_file()

    # a file that is gone entirely → a clear 400, not a silent miss
    with pytest.raises(HTTPException) as ei:
        _common.resolve_inbound_path(str(ws / "01_input" / "gone.txt"))
    assert ei.value.status_code == 400

    # a workspace-relative value resolves against the current root
    p2 = _common.resolve_inbound_path("01_input/book.txt")
    assert p2 == ws_new / "01_input" / "book.txt"


def test_require_workspace_reports_missing_dir(sandbox):
    """A pointer to a deleted/moved folder yields a clear, actionable error (409)."""
    from backend.api import _common

    ws = sandbox / "Gone" / "ws"
    (ws / "01_input").mkdir(parents=True)
    _set_pointer(sandbox, ws)
    shutil.rmtree(ws)

    with pytest.raises(HTTPException) as ei:
        _common.require_workspace()
    assert ei.value.status_code == 409
    assert "重新选择" in str(ei.value.detail)  # actionable: re-select on the dashboard


def test_workspace_config_working_dir_self_heals(sandbox):
    """The workspace's config/setting.json keeps a copy of the pointer. After the folder
    moves and is re-selected, reads report the LIVE pointer (in memory) and the next
    save persists it — the stale value never lingers."""
    ws = sandbox / "OldPlace" / "ws"
    ws.mkdir(parents=True)
    _set_pointer(sandbox, ws)
    core_config.init_workspace_config(ws)  # seeds ws/config/setting.json with working_dir = ws
    assert core_config.get_config().paths.working_dir == str(ws)

    ws_new = sandbox / "NewPlace" / "ws"
    ws_new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ws), str(ws_new))
    _set_pointer(sandbox, ws_new)

    # read: the in-memory config reports the live pointer despite the stale file
    assert core_config.get_config().paths.working_dir == str(ws_new)
    # the file on disk still holds the stale value until a save …
    on_disk = json.loads((ws_new / "config" / "setting.json").read_text("utf-8"))
    assert on_disk["paths"]["working_dir"] == str(ws)
    # … and the next save (requirement: migrate on save) rewrites it
    core_config.update_config({})
    on_disk = json.loads((ws_new / "config" / "setting.json").read_text("utf-8"))
    assert on_disk["paths"]["working_dir"] == str(ws_new)
