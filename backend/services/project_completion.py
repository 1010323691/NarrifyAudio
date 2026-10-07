"""Read project-wide production coverage; completed runs are not book completion."""
from __future__ import annotations

import json
import math
import time
from functools import lru_cache
from pathlib import Path

from ..core.paths import Layout
from ..core.safe_filesystem import is_link_or_junction
from ..core.request_context import bind_workspace, reset_workspace
from ..engines import book, tts_batch as batch
from .project_filesystem import iter_regular_project_files


COMPLETION_SECTIONS = {"text", "catalog", "production"}


def _regular_file(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
        current = root
        if is_link_or_junction(current):
            return False
        for part in relative.parts:
            current = current / part
            if is_link_or_junction(current):
                return False
        return path.is_file() and path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


@lru_cache(maxsize=64)
def _completion_window(root: str, section: str, window: int) -> dict:
    return project_completion(Path(root), section=section)


def overview_completion(root: Path, section: str) -> dict:
    # Bounded, read-only summaries in five-second windows; never used by Workers.
    return _completion_window(str(root), section, int(time.monotonic() // 5))


def project_completion(root: Path, *, section: str | None = None) -> dict:
    if section is not None and section not in COMPLETION_SECTIONS:
        raise ValueError("无效的进度区域")
    directories = ["01_input", "02_split_text"]
    if section != "text":
        directories.append("03_parsed_json")
    if section not in {"text", "catalog"}:
        directories.extend(["06_audio_merge", "08_bgm"])
    files = {f"{directory}/{relative.as_posix()}": path
             for directory in directories
             for path, relative, _ in iter_regular_project_files(root / directory)} if not is_link_or_junction(root) else {}
    voice_path = root / "04_voice_profiles/voice_config.json"
    if section != "text" and _regular_file(voice_path, root):
        files["04_voice_profiles/voice_config.json"] = voice_path

    def read(name: str):
        path = files.get(name)
        if path is None:
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def ratio(done: int, total: int, unit: str, *, known: bool = True):
        return {"completed": done, "total": total, "unit": unit,
                "percent": min(100, math.floor(done / total * 100)) if known and total else (0 if known else None)}

    split = {Path(name).stem for name in files
             if name.startswith("02_split_text/") and name.endswith(".txt")}
    scripts = {}
    for name in files:
        if name.startswith("03_parsed_json/") and name.endswith(".json") and not name.endswith("_checked.json"):
            data = read(name)
            if isinstance(data, list) and data and all(
                isinstance(entry, dict) and all(entry.get(key) is None or isinstance(entry.get(key), str)
                                               for key in ("text", "speaker", "type", "instruct"))
                for entry in data
            ):
                scripts[Path(name).stem] = data
    chapters = split | scripts.keys()
    total_chapters = len(chapters)
    # Source chapter detection supplies a denominator even before any split is written.
    originals = [path for name, path in files.items()
                 if name.startswith("01_input/") and name.endswith(".txt") and not path.stem.endswith("_排版")]
    expected_split = len(split)
    split_total_known = True
    if originals:
        try:
            source = max(originals, key=lambda path: path.stat().st_mtime_ns)
            text, _encoding = book.decode_buffer(source.read_bytes())
            expected_split = max(expected_split, len(book.detect_chapters(text)) or 1)
        except (OSError, ValueError):
            # Keep known generated chapters; never erase their denominator on
            # a source decoding/read error. With no known chapters, mark the
            # percentage unknown instead of claiming an inaccurate 0%.
            split_total_known = expected_split > 0
    total_chapters = max(total_chapters, expected_split)
    text_completion = ratio(len(split), expected_split, "章节", known=split_total_known)
    if section == "text":
        return {"02_split_text": text_completion}
    voices = read("04_voice_profiles/voice_config.json")
    voices = voices if isinstance(voices, dict) else {}
    speakers = {str(entry.get("speaker") or entry.get("type") or "").strip()
                for data in scripts.values() for entry in data} - {""}

    def voice_ready(name: str):
        entry = voices.get(name) or {}
        return isinstance(entry, dict) and bool(
            entry.get("type") == "custom"
            or (entry.get("type") == "clone" and entry.get("ref_audio"))
            or (entry.get("type") == "design" and str(entry.get("description") or "").strip()))

    parsed_all = total_chapters > 0 and len(scripts) == total_chapters
    catalog = {
        "03_parsed_json": ratio(len(scripts), total_chapters, "章节"),
        "04_voice_profiles": ratio(sum(voice_ready(name) for name in speakers), len(speakers), "角色", known=parsed_all or not total_chapters),
    }
    if section == "catalog":
        return catalog
    segment_total = segment_done = merged = mixed = 0
    layout = Layout(root)
    token = bind_workspace(root)
    try:
        for stem, data in scripts.items():
            segments = batch.build_segments(data)
            manifest_name = f"05_audio_chunk/{stem}/manifest.json"
            manifest = batch.read_manifest(layout.audio_chunk / stem) if _regular_file(root / manifest_name, root) else {}
            expected = batch.segment_voice_params(segments, voices) if "04_voice_profiles/voice_config.json" in files else None
            done_indices = batch.done_indices(manifest, layout.audio_chunk / stem, root,
                                              expected_voice_params=expected)
            done_indices = {index for index in done_indices
                            if _safe_manifest_audio(manifest.get(index), root)}
            done = sum(segment["index"] in done_indices for segment in segments)
            segment_total += len(segments)
            segment_done += done
            # Match the merge workbench: old merged audio is invalid when synthesis is incomplete.
            complete = bool(segments) and done == len(segments)
            merge_exists = f"06_audio_merge/{stem}.mp3" in files
            merged += int(complete and merge_exists)
            mixed += int(complete and merge_exists and f"08_bgm/{stem}.mp3" in files)
    finally:
        reset_workspace(token)
    production = {
        "05_audio_chunk": ratio(segment_done, segment_total, "段", known=parsed_all or not total_chapters),
        "06_audio_merge": ratio(merged, total_chapters, "章节"),
        "08_bgm": ratio(mixed, total_chapters, "章节"),
        # Episode totals require the chosen splitting plan; a file alone cannot establish 100%.
        "07_output": ratio(0, 0, "分集", known=False),
    }
    return production if section == "production" else {"02_split_text": text_completion, **catalog, **production}


def _safe_manifest_audio(entry, root: Path, safe_files: set[Path] | None = None) -> bool:
    from ..core import pathio

    try:
        path = pathio.resolve_path((entry or {}).get("path", ""), root, strict=False)
        return path is not None and (path in safe_files if safe_files is not None else _regular_file(path, root))
    except (OSError, ValueError, pathio.PathOutsideWorkspace):
        return False
