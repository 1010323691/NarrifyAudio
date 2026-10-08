import json
from pathlib import Path

from backend.core.manifest_speakers import candidate_manifests
from backend.core import role_hint_cache


def test_warm_manifest_lookup_reads_only_changed_chapters(tmp_path, monkeypatch):
    root = tmp_path / "audio"
    for index in range(100):
        folder = root / str(index)
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps([{"speaker": f"role-{index}"}]), encoding="utf-8")
    assert candidate_manifests(root, ["role-0"]) == [root / "0" / "manifest.json"]
    original, reads = Path.read_text, []
    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert candidate_manifests(root, ["role-99"]) == [root / "99" / "manifest.json"]
    assert not reads
    changed = root / "50" / "manifest.json"
    changed.write_text(json.dumps([{"speaker": "role-99"}]), encoding="utf-8")
    assert candidate_manifests(root, ["role-99"]) == [changed, root / "99" / "manifest.json"]
    assert reads == [changed]


def test_hint_summary_cache_ignores_audio_only_changes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(role_hint_cache, "suggest_role_hints", lambda *args: calls.append(args) or {"小熊猫": "熊猫"})
    names = ["熊猫", "小熊猫"]
    config = {name: {"description": "青年男性", "gender": "male", "ref_audio": "first.wav"} for name in names}
    assert role_hint_cache.cached_role_hints(names, config, {}, tmp_path) == {"小熊猫": "熊猫"}
    config["熊猫"]["ref_audio"] = "second.wav"
    role_hint_cache.cached_role_hints(names, config, {}, tmp_path)
    assert len(calls) == 1
    config["熊猫"]["description"] = "老年女性"
    role_hint_cache.cached_role_hints(names, config, {}, tmp_path)
    assert len(calls) == 2


def test_corrupt_manifest_index_is_rebuilt_without_losing_candidates(tmp_path):
    root = tmp_path / "audio"
    path = root / "chapter" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"speaker": "A"}]), encoding="utf-8")
    assert candidate_manifests(root, ["A"]) == [path]
    (root / ".speaker-index.sqlite").write_bytes(b"broken index")
    assert candidate_manifests(root, ["A"]) == [path]
    assert list(root.glob(".speaker-index.corrupt-*.sqlite"))
