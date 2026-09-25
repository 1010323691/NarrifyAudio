"""Voice identity and persisted synthesis-manifest operations."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from ..core import pathio
from ..core.paths import get_or_prepare_layout, resolve_layout


def _canonical_voice_name(speaker: str, voice_config: dict) -> str:
    """Resolve the same alias chain that the TTS worker uses."""
    name = (speaker or "").strip()
    seen = set()
    for _ in range(8):
        if not name or name in seen:
            break
        seen.add(name)
        entry = voice_config.get(name) or {}
        alias = entry.get("alias_of") or entry.get("alias")
        if not isinstance(alias, str) or not alias.strip() or alias == name:
            break
        name = alias.strip()
    return name


def _canonical_voice_path(value) -> str:
    """Normalize an in-workspace voice path before hashing it.

    ``voice_config.json`` is lazily migrated from absolute paths to workspace-relative
    paths. The representation change must not look like a new voice after a restart or
    workspace move, while the path below ``04_voice_profiles`` must remain part of the
    identity so two different reference recordings do not collide.
    """
    if not isinstance(value, str):
        return ""
    path = value.strip().replace("\\", "/")
    marker = "/04_voice_profiles/"
    folded = path.casefold()
    marker_at = folded.find(marker)
    if marker_at >= 0:
        path = "04_voice_profiles/" + path[marker_at + len(marker):]
    while path.startswith("./"):
        path = path[2:]
    return path.casefold() if os.name == "nt" else path


def voice_params(speaker: str, voice_config: dict) -> dict:
    """Return the effective JSON voice parameters used for one script speaker.

    This object is persisted in each successful manifest entry as ``voice_used``.  It is
    intentionally made from synthesis inputs only; UI-only state such as the gender badge
    and clone progress markers must not invalidate already rendered speech.
    """
    canonical = _canonical_voice_name(speaker, voice_config)
    entry = voice_config.get(canonical) or {}
    return {
        "canonical": canonical,
        "type": entry.get("type", ""),
        "ref_audio": _canonical_voice_path(entry.get("ref_audio", "")),
        "ref_text": entry.get("ref_text", ""),
        "description": entry.get("description", ""),
        "voice": entry.get("voice", ""),
        "instruct": entry.get("instruct", ""),
    }


def voice_signature(speaker: str, voice_config: dict) -> str:
    """Return the legacy stable hash for the effective voice parameters.

    New manifests persist :func:`voice_params` itself.  The hash remains in manifests for
    compatibility with older tooling and for migrating old tests/projects.
    """
    payload = voice_params(speaker, voice_config)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def segment_voice_params(segments, voice_config: dict) -> dict[int, dict]:
    """Map each segment index to the exact effective voice JSON for that segment."""
    return {s["index"]: voice_params(s.get("speaker", ""), voice_config)
            for s in segments}


def segment_voice_signatures(segments, voice_config: dict) -> dict[int, str]:
    """Map each segment index to the effective voice signature for that segment."""
    return {s["index"]: voice_signature(s.get("speaker", ""), voice_config)
            for s in segments}


def _voice_signature_matches(entry: dict, expected: str | None) -> bool:
    """Check that a manifest entry was rendered with the current voice inputs.

    Once a workspace has a voice configuration, an entry without a signature is
    deliberately stale. Treating it as complete would let a process restart
    resurrect legacy progress and silently reuse audio rendered before a voice edit.
    ``expected is None`` remains the compatibility path for projects without any
    voice configuration.
    """
    return expected is None or entry.get("voice_signature") == expected


def _voice_params_match(entry: dict, expected: dict | None) -> bool:
    """Compare the current target JSON with the voice JSON actually used by the segment."""
    return expected is None or entry.get("voice_used") == expected


def _resolved_existing_path(value, workspace):
    """Resolve a manifest path and return it only when the generated file still exists."""
    if not value:
        return None
    try:
        path = pathio.resolve_path(value, workspace, strict=False) if workspace is not None else Path(value)
    except (OSError, TypeError, pathio.PathOutsideWorkspace, pathio.PathNotFoundError):
        return None
    if path is None:
        return None
    try:
        return path if path.exists() else None
    except OSError:
        return None


def _archive_voice_version(entry: dict, workspace, handle=None) -> bool:
    """Keep the current generated file under a voice-specific name before invalidating it."""
    path = _resolved_existing_path(entry.get("path"), workspace)
    signature = entry.get("voice_signature") or ""
    used = entry.get("voice_used")
    if path is None or (not signature and (not isinstance(used, dict) or not used)):
        return False
    versions = entry.setdefault("voice_versions", [])
    if not isinstance(versions, list):
        versions = []
        entry["voice_versions"] = versions
    for version in versions:
        if not isinstance(version, dict):
            continue
        same_voice = (
            signature and version.get("voice_signature") == signature
            or isinstance(used, dict) and used and version.get("voice_used") == used
        )
        if same_voice and _resolved_existing_path(version.get("path"), workspace) is not None:
            return False
    token = signature[:16] if signature else hashlib.sha256(
        json.dumps(used, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    archived = path.with_name(f"{path.stem}.voice-{token}{path.suffix}")
    try:
        if not archived.exists():
            stage_workspace_copy = getattr(handle, "stage_workspace_copy", None)
            if callable(stage_workspace_copy):
                stage_workspace_copy(archived, path)
            else:
                shutil.copy2(path, archived)
    except OSError:
        return False
    versions.append({
        "path": _store_path(str(archived), workspace),
        "voice_signature": signature,
        **({"voice_used": used} if isinstance(used, dict) else {}),
    })
    return True


def _restore_cached_voice_versions(entries: dict, expected_voice_params: dict | None,
                                   expected_voice_signatures: dict | None,
                                   workspace) -> int:
    """Restore a previously synthesized voice version into the active manifest entry."""
    if expected_voice_params is None:
        return 0
    restored = 0
    for index, entry in entries.items():
        if not isinstance(entry, dict) or _voice_params_match(entry, expected_voice_params.get(index)):
            continue
        expected_params = expected_voice_params.get(index)
        expected_signature = ((expected_voice_signatures or {}).get(index)
                              if expected_voice_signatures is not None else None)
        versions = entry.get("voice_versions")
        if not isinstance(versions, list):
            continue
        for version in reversed(versions):
            if not isinstance(version, dict):
                continue
            if version.get("voice_used") is not None:
                matches = version.get("voice_used") == expected_params
            else:
                matches = bool(expected_signature) and version.get("voice_signature") == expected_signature
            if not matches:
                continue
            path = _resolved_existing_path(version.get("path"), workspace)
            if path is None:
                continue
            entry["path"] = version["path"]
            entry["ok"] = True
            entry["reason"] = ""
            entry["voice_used"] = expected_params
            if expected_signature is not None:
                entry["voice_signature"] = expected_signature
            restored += 1
            break
    return restored


_FORBIDDEN_PACKAGE_CHARS = '\\/:*?"<>|'
_DEFAULT_PACKAGE_NAME = "audiobook"


def _safe_package_name(name: str) -> str:
    """Filesystem-safe package name for merge outputs and bgm side files.

    Do not swap in ``platform.storage.safe_display_name``: it takes
    ``Path(name).name``, truncates at 180 chars and strips dots, so files that
    already exist on disk would change name.
    """
    safe = "".join("_" if c in _FORBIDDEN_PACKAGE_CHARS else c for c in name).strip()
    return safe or _DEFAULT_PACKAGE_NAME


def _merged_output_paths(layout, package: str):
    """Generated merge outputs for a package (only exact, derived file names)."""
    safe = _safe_package_name(package)
    return [layout.audio_merge / f"{safe}.mp3", layout.audio_merge / f"{safe}.wav"]


def _defer_or_delete(handle, path: Path) -> None:
    defer_delete = getattr(handle, "defer_workspace_delete", None)
    if callable(defer_delete):
        defer_delete(path)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _migrate_voice_config(handle, path: Path, workspace, voice_config: dict) -> None:
    changed = pathio.migrate_entries(voice_config.values(), workspace, ("ref_audio",))
    if not changed:
        return
    encoded = json.dumps(voice_config, ensure_ascii=False, indent=2).encode("utf-8")
    stage_file = getattr(handle, "publish_workspace_bytes", None) or getattr(handle, "stage_workspace_file", None)
    if callable(stage_file):
        stage_file(path, encoded)
    else:
        path.write_bytes(encoded)


def invalidate_speaker_outputs(speakers, layout=None, *, handle=None) -> int:
    """Mark old per-segment audio for changed speakers as stale and drop merged output.

    This is intentionally limited to generated ``05_audio_chunk/<package>`` manifests
    and the exact derived merge files.  Audio files remain on disk for inspection, but
    their manifest entries can no longer satisfy a resume or merge-readiness check.
    """
    layout = layout or get_or_prepare_layout()
    if layout.audio_chunk is None:
        return 0
    names = {str(s).strip() for s in (speakers or []) if str(s).strip()}
    if not names:
        return 0
    changed = 0
    for manifest_path in layout.audio_chunk.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        touched = False
        for entry in data:
            if isinstance(entry, dict) and (entry.get("speaker") or "").strip() in names:
                if entry.get("voice_used") != {} or entry.get("voice_signature") != "":
                    _archive_voice_version(entry, layout.workspace, handle)
                    entry["voice_used"] = {}
                    entry["voice_signature"] = ""
                    touched = True
                    changed += 1
        if touched:
            stage_workspace_file = getattr(handle, "publish_workspace_bytes", None) or getattr(handle, "stage_workspace_file", None)
            if callable(stage_workspace_file):
                stage_workspace_file(
                    manifest_path,
                    json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                )
            else:
                pathio.rewrite_json_file(manifest_path, data)
            outputs = _merged_output_paths(layout, manifest_path.parent.name)
            if layout.bgm is not None:
                safe = _safe_package_name(manifest_path.parent.name)
                outputs.append(layout.bgm / f"{safe}.mp3")
            for output in outputs:
                defer_workspace_delete = getattr(handle, "defer_workspace_delete", None)
                if callable(defer_workspace_delete):
                    defer_workspace_delete(output)
                    continue
                try:
                    output.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
    return changed




def package_for(src: Path) -> str:
    """The package (sub-folder in ``05_audio_chunk/``) a source JSON's batch output lands in.

    Named after the source's base stem so 音频合并 can list & pick a package; a base and its
    ``_checked`` variant share the same package.
    """
    stem = src.stem
    if stem.endswith("_checked"):
        stem = stem[: -len("_checked")]
    return stem or "batch"


def _migrate_legacy_voice_used(data: list, layout) -> int:
    """Recover ``voice_used`` for manifests written before the per-segment JSON field.

    A legacy entry is safe to migrate only when its stored signature exactly matches the
    current target parameters.  A mismatched or signature-less entry remains stale and must
    be synthesized again.
    """
    vc_path = layout.voice_profiles / "voice_config.json"
    if not vc_path.exists():
        return 0
    try:
        voice_config = json.loads(vc_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(voice_config, dict):
        return 0
    migrated = 0
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("ok") or "voice_used" in entry:
            continue
        speaker = entry.get("speaker", "")
        expected = voice_signature(speaker, voice_config)
        if entry.get("voice_signature") == expected:
            entry["voice_used"] = voice_params(speaker, voice_config)
            migrated += 1
    return migrated


def read_manifest(out_dir) -> dict:
    """Read a manifest with legacy entries normalized in memory, without writing it."""
    return _load_manifest(out_dir, persist_migration=False)


def migrate_manifest(out_dir, handle=None) -> dict:
    """Read and persist legacy manifest migrations for a write operation."""
    return _load_manifest(out_dir, persist_migration=True, handle=handle)


load_manifest = migrate_manifest  # compatibility for existing Python callers


def _load_manifest(out_dir, *, persist_migration: bool, handle=None) -> dict:
    """The package's cumulative manifest as ``{index: entry}`` (``{}`` if absent / unreadable).

    One entry per segment the batch has ever reported — the source of truth for what is already
    synthesized, so a cancel (or a later re-run) can resume without re-doing finished work.
    """
    p = Path(out_dir) / "manifest.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt manifest just means "start fresh"
        return {}
    if not isinstance(data, list):
        return {}
    # Lazy migration (single read): legacy manifests stored absolute paths (the workspace's
    # location at write time). Convert any that still point inside the workspace to the
    # relative form and rewrite the file, so the project keeps working after the workspace
    # moves. ``migrate_entries`` + ``rewrite_json_file`` on the already-parsed list is the
    # in-memory form of ``migrate_entries_in`` (which would re-read the same file).
    layout = get_or_prepare_layout() if persist_migration else resolve_layout()
    n = pathio.migrate_entries(data, layout.workspace, ("path",))
    voice_migrated = _migrate_legacy_voice_used(data, layout)
    by_index = {}
    for e in data:
        if isinstance(e, dict) and "index" in e:
            try:
                by_index[int(e["index"])] = e
            except (TypeError, ValueError):
                pass
    expected_params = {}
    expected_signatures = {}
    vc_path = layout.voice_profiles / "voice_config.json"
    if vc_path.exists():
        try:
            voice_config = json.loads(vc_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            voice_config = None
        if isinstance(voice_config, dict):
            for index, entry in by_index.items():
                if isinstance(entry, dict):
                    speaker = entry.get("speaker", "")
                    expected_params[index] = voice_params(speaker, voice_config)
                    expected_signatures[index] = voice_signature(speaker, voice_config)
    restored = _restore_cached_voice_versions(
        by_index, expected_params or None, expected_signatures or None, layout.workspace,
    )
    if persist_migration and (n or voice_migrated or restored):
        encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        stage_file = getattr(handle, "publish_workspace_bytes", None) or getattr(handle, "stage_workspace_file", None)
        if callable(stage_file):
            stage_file(p, encoded)
        else:
            p.write_bytes(encoded)
    return by_index


def is_done(entry, expected_voice_signature: str | None = None,
            expected_voice_params: dict | None = None) -> bool:
    """Whether a manifest entry is a *completed* segment: ``ok`` AND its file still on disk.

    A missing file (deleted, or a run killed between writing the file and its line being
    drained) means the segment is not truly done, so a resume re-synthesizes it. The path
    value is resolved against the *current* workspace root (relative form; a legacy
    absolute value still works, and one invalidated by a workspace move is recovered
    best-effort) — never against a fixed location.
    """
    if not entry or not entry.get("ok"):
        return False
    if not _voice_signature_matches(entry, expected_voice_signature):
        return False
    if not _voice_params_match(entry, expected_voice_params):
        return False
    path = entry.get("path")
    if not path:
        return False
    ws = resolve_layout().workspace
    try:
        p = pathio.resolve_path(path, ws, strict=False) if ws is not None else Path(path)
    except (OSError, TypeError, pathio.PathOutsideWorkspace, pathio.PathNotFoundError):
        return False
    if p is None:
        return False
    try:
        return p.exists()
    except (OSError, TypeError):
        return False


def done_indices(old_entries: dict, out_dir, ws,
                 expected_voice_signatures: dict[int, str] | None = None,
                 expected_voice_params: dict[int, dict] | None = None) -> set[int]:
    """The done manifest entries (``ok`` AND the file still on disk) as a set of indices.

    The same judgment as :func:`is_done`, batched for a whole package: entries whose stored
    path lives directly inside ``out_dir`` (the normal relative form
    ``05_audio_chunk/<pkg>/NNNN.mp3``) are answered from ONE lazy directory listing instead
    of one stat per entry; every other shape (legacy absolute, external, ``..``, another
    directory, or no workspace) falls back to the exact per-entry :func:`is_done`, so the
    result is identical entry-for-entry. A missing ``out_dir`` yields ``{}`` (nothing
    exists — the same as per-entry ``exists()`` on a gone directory).
    """
    done: set[int] = set()

    def current(i, entry) -> bool:
        expected = ((expected_voice_signatures or {}).get(i)
                    if expected_voice_signatures is not None else None)
        voice = ((expected_voice_params or {}).get(i)
                 if expected_voice_params is not None else None)
        return is_done(entry, expected, voice)

    rel_out = pathio.to_workspace_relative(str(out_dir), ws) if ws is not None else None
    if rel_out is None:
        for i, e in old_entries.items():
            if current(i, e):
                done.add(i)
        return done
    # NTFS is case-insensitive (exists() folds case); POSIX is not.
    fold = str.casefold if os.name == "nt" else (lambda s: s)
    prefix = rel_out + "/"
    names: set | None = None  # lazy: one os.listdir for the whole package
    for i, e in old_entries.items():
        if not (isinstance(e, dict) and e.get("ok")):
            continue
        expected = ((expected_voice_signatures or {}).get(i)
                    if expected_voice_signatures is not None else None)
        voice = ((expected_voice_params or {}).get(i)
                 if expected_voice_params is not None else None)
        if not _voice_signature_matches(e, expected) or not _voice_params_match(e, voice):
            continue
        v = e.get("path")
        if not isinstance(v, str) or not v.strip():
            continue
        if not pathio._is_abs(v):
            vn = pathio._norm(v)
            if not pathio._escapes(vn) and vn.startswith(prefix):
                base = vn[len(prefix):]
                if base and "/" not in base:
                    if names is None:
                        try:
                            # No is_file filter on purpose: Path.exists() is True for a
                            # directory named like an mp3 too — the set must match it.
                            names = {fold(n) for n in os.listdir(out_dir)}
                        except OSError:
                            names = None
                    if names is not None and fold(base) in names:
                        done.add(i)
                        continue
        if current(i, e):  # legacy / external / escaping value: the exact per-entry rule
            done.add(i)
    return done


def plan_to_synthesize(all_indices, done_set, indices=None):
    """Which line indices a run should synthesize (a subset of ``all_indices``).

    An explicit ``indices`` wins (synthesise exactly those, intersected with the valid set);
    otherwise the default is a *resume* — only the not-yet-done lines (``all - done``).
    Re-doing everything is NOT a planning mode: the caller deletes the package folder first
    (``POST /api/tts/batch-reset``), after which an ordinary resume has nothing to skip.
    """
    all_set = set(all_indices)
    if indices:
        return {int(i) for i in indices} & all_set
    return all_set - set(done_set)


def _store_path(path, root):
    """The manifest's on-disk form of an audio path: workspace-relative when the file
    lives inside the workspace (the location-independent form), the value unchanged when
    it does not (an external resource keeps its absolute path)."""
    if not path or root is None:
        return path
    rel = pathio.to_workspace_relative(path, root)
    return path if rel is None else rel


def _preserve_voice_versions(item: dict, old) -> dict:
    """Carry the per-voice audio cache through every incremental manifest rewrite."""
    if isinstance(old, dict) and isinstance(old.get("voice_versions"), list):
        item["voice_versions"] = old["voice_versions"]
    return item


def build_manifest(all_segments, old_entries, run_results, root=None,
                   expected_voice_signatures: dict[int, str] | None = None,
                   expected_voice_params: dict[int, dict] | None = None):
    """Rebuild the package manifest: one entry per non-empty segment, in index order.

    For each segment this run's result wins; else a prior *done* entry is preserved (its existing
    audio path, paired with the current script's speaker/text/pause); else a not-done entry
    (``ok: false``, empty path) reusing a prior failure's reason when there is one. The same
    function powers both the incremental (per-segment) writes and the final write, so the file
    always holds the cumulative state and a cancel never loses finished work.

    ``root`` (the workspace root) gives every stored path the location-independent,
    workspace-relative form; with ``None`` (or for out-of-workspace paths) the value is
    stored as given. Passing the live root also migrates any legacy absolute value a
    preserved old entry still carries.
    """
    manifest = []
    for s in sorted(all_segments, key=lambda x: x["index"]):
        index = s["index"]
        base = {
            "index": index,
            "speaker": s["speaker"],
            "text": s["text"],
            "pause_after": s["pause_after"],
        }
        if expected_voice_signatures is not None:
            base["voice_signature"] = expected_voice_signatures.get(index, "")
        r = run_results.get(index)
        if r is not None:
            if r.get("ok"):
                item = {**base, "path": _store_path(r.get("path", ""), root),
                        "ok": True, "reason": ""}
                if expected_voice_params is not None:
                    item["voice_used"] = expected_voice_params.get(index, {})
                manifest.append(_preserve_voice_versions(item, old_entries.get(index)))
            else:
                item = {**base, "path": "", "ok": False, "reason": r.get("reason", "")}
                old = old_entries.get(index)
                if isinstance(old, dict) and "voice_used" in old:
                    item["voice_used"] = old["voice_used"]
                manifest.append(_preserve_voice_versions(item, old_entries.get(index)))
        else:
            old = old_entries.get(index)
            if old is not None and is_done(
                old,
                expected_voice_signatures.get(index) if expected_voice_signatures is not None else None,
                expected_voice_params.get(index) if expected_voice_params is not None else None,
            ):
                item = {**base, "path": _store_path(old.get("path", ""), root),
                        "ok": True, "reason": ""}
                if "voice_used" in old:
                    item["voice_used"] = old["voice_used"]
                manifest.append(_preserve_voice_versions(item, old))
            elif old is not None:
                item = {**base, "path": "", "ok": False, "reason": old.get("reason", "")}
                if "voice_used" in old:
                    item["voice_used"] = old["voice_used"]
                manifest.append(_preserve_voice_versions(item, old))
            else:
                manifest.append({**base, "path": "", "ok": False, "reason": ""})
    return manifest


def count_completion(all_segments, manifest_by_index, out_dir=None,
                     expected_voice_signatures: dict[int, str] | None = None,
                     expected_voice_params: dict[int, dict] | None = None) -> dict:
    """``{total, completed, remaining}`` for a script — completed = done (ok + file exists).

    ``total`` is the number of non-empty (synthesizable) segments; the ``synthesize`` result and
    the read-only ``/batch-status`` endpoint both derive their numbers from this.

    ``out_dir`` (the package dir) switches the existence check to the batched
    :func:`done_indices` (one directory listing for every ``ok`` entry instead of one stat per
    entry — identical judgment); omitted, the per-entry :func:`is_done` loop runs verbatim.
    """
    total = len(all_segments)
    if out_dir is not None:
        done = done_indices(manifest_by_index, out_dir, resolve_layout().workspace,
                            expected_voice_signatures, expected_voice_params)
        completed = sum(1 for s in all_segments if s["index"] in done)
    else:
        completed = sum(
            1 for s in all_segments
            if is_done(
                manifest_by_index.get(s["index"]),
                expected_voice_signatures.get(s["index"])
                if expected_voice_signatures is not None else None,
                expected_voice_params.get(s["index"])
                if expected_voice_params is not None else None,
            )
        )
    return {"total": total, "completed": completed, "remaining": total - completed}


def _write_manifest_file(manifest_path, manifest, handle=None) -> None:
    """Write the (list) manifest to disk (UTF-8, pretty-printed; JSON tolerates the CRLF)."""
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    stage_file = getattr(handle, "publish_workspace_bytes", None) or getattr(handle, "stage_workspace_file", None)
    if callable(stage_file):
        stage_file(manifest_path, encoded)
    else:
        manifest_path.write_bytes(encoded)
