"""Music library endpoints (背景音乐系统 · 音乐库).

The library is a GLOBAL resource (project root, outside any workspace), so NO
endpoint here calls ``require_workspace`` — the page works with the pipeline
locked. The only workspace-aware part is the delete guard: a track referenced
by a LOCKED chapter of the *current* workspace is skipped (unlocked references
are not blocked — they degrade to ``music_missing`` and self-heal on re-match).
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import paths as core_paths
from ..core.config import get_config
from ..engines import music as music_engine
from ..engines.audio import probe_duration
from ..platform.config import settings
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context, require_admin
from ..platform.models import User, Project
from ..platform.engine_task_submission import active_durable_targets, submit_legacy_engine_task
from ..platform.storage import configured_storage_root, safe_display_name

router = APIRouter(prefix="/api/music", tags=["music"])

MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac"}

# Batch AI tag recognition label contract (承重 — backend regex ↔ frontend
# derivation ↔ tests): module ``music-ai-tags`` with label ``AI 推荐标签：{name}``.
AI_TAGS_MODULE = "music-ai-tags"
AI_TAGS_LABEL = "AI 推荐标签"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _library_dir() -> Path:
    # Call-time module attribute access (tests monkeypatch core_paths.MUSIC_LIBRARY_DIR).
    return core_paths.MUSIC_LIBRARY_DIR


def _track_path(name: str) -> Path:
    """Guard + resolve a music file name inside the library (traversal -> 400)."""
    try:
        bare = music_engine.validate_music_name(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    d = _library_dir().resolve()
    p = (d / bare).resolve()
    if not str(p).startswith(str(d)):
        raise HTTPException(400, "非法路径")
    return p


def _track_size(name: str) -> int | None:
    try:
        path = _track_path(name)
        return path.stat().st_size if path.is_file() and not path.is_symlink() else None
    except OSError:
        return None


def _locked_references(name: str) -> list[str]:
    """Current workspace's chapter stems whose assignment is LOCKED and points
    at ``name`` (from ``08_bgm/bgm_assignments.json``). No workspace / no file
    -> no references. Read-only, best-effort (a corrupt file degrades to [])."""
    from ..core.paths import resolve_layout

    layout = resolve_layout()
    bgm = layout.bgm
    if bgm is None or not bgm.exists():
        return []
    f = bgm / "bgm_assignments.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_bytes().decode("utf-8"))
        chapters = data.get("chapters") if isinstance(data, dict) else None
        if not isinstance(chapters, dict):
            return []
        return [
            stem for stem, ch in chapters.items()
            if isinstance(ch, dict) and ch.get("locked") and ch.get("music") == name
        ]
    except Exception:
        return []


def _delete_track(name: str) -> tuple[bool, str]:
    """Delete one track (file + index entry). Returns ``(deleted, skip_reason)``.
    LOCKED chapter references block the delete (skipped, not an error);
    unlocked references are left to degrade (``music_missing``)."""
    refs = _locked_references(name)
    if refs:
        shown = ", ".join(refs[:3]) + ("…" if len(refs) > 3 else "")
        return False, f"被 {len(refs)} 章锁定引用（{shown}）"
    p = _track_path(name)  # also validates (400 on traversal)
    if p.exists():
        p.unlink()
    music_engine.update_index(
        lambda idx: idx["tracks"].pop(name, None)
    )
    return True, ""


# --------------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------------- #

class TrackUpdate(BaseModel):
    tags: dict | None = None
    enabled: bool | None = None
    description: str | None = None


class BatchNames(BaseModel):
    names: list[str]
    enabled: bool | None = None  # batch-enable only


class BatchTags(BaseModel):
    tracks: list[str]  # 选中音乐（文件名）
    names: list[str]  # 标签名
    category: str
    op: str = "add"  # add | remove


class TagCreate(BaseModel):
    category: str
    name: str


class TagRename(BaseModel):
    category: str
    name: str
    new_name: str


class SuggestTagsReq(BaseModel):
    name: str
    description: str | None = None


class ApplySuggestionsReq(BaseModel):
    names: list[str]


# --------------------------------------------------------------------------- #
# library
# --------------------------------------------------------------------------- #

@router.get("/library")
def get_library(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
    """The full index (incl. the ``folders`` section + each track's ``folder``
    field) + ``folder_counts`` + pending AI suggestions (read-only).

    ``suggestions`` = the candidates from the batch AI tag recognition
    (``music_tag_suggestions.json``) filtered to EXISTING tracks (orphans of
    deleted tracks are hidden, never removed). Only UN-adopted candidates
    linger here — the worker auto-adopts (and consumes) the candidate of a
    still-untagged track; entries remain when the track already has manual
    tags (never overwritten) or the LLM suggested none.
    """
    idx = music_engine.load_index()
    sugg = music_engine.load_suggestions().get("tracks") or {}
    usage_counts: dict[str, int] = {}
    is_admin = getattr(getattr(ctx, "user", None), "role", None) == "admin"
    if is_admin and hasattr(db, "execute"):
        root = configured_storage_root(db).resolve()
        workspaces = db.execute(
            select(Project, User.username).join(User, User.id == Project.owner_id)
            .where(Project.deleted_at.is_(None))
        ).all()
        for workspace, username in workspaces:
            workspace_root = root / safe_display_name(username) / workspace.id
            if workspace_root.is_symlink() or workspace_root.parent.is_symlink():
                continue
            try:
                if not workspace_root.resolve().is_relative_to(root):
                    continue
                assignments = workspace_root / "08_bgm" / "bgm_assignments.json"
                if assignments.is_symlink() or assignments.parent.is_symlink() or not assignments.is_file() or assignments.stat().st_size > 5 * 1024 * 1024:
                    continue
                data = json.loads(assignments.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
                continue
            chapters = data.get("chapters") if isinstance(data, dict) else None
            if not isinstance(chapters, dict):
                continue
            for entry in chapters.values():
                if isinstance(entry, dict) and isinstance(entry.get("music"), str):
                    music_name = entry["music"]
                    usage_counts[music_name] = usage_counts.get(music_name, 0) + 1
    tracks = {
        name: {**track, "size_bytes": _track_size(name),
               "use_count": usage_counts.get(name, 0) if is_admin else None}
        for name, track in idx["tracks"].items()
    }
    return {**idx, "tracks": tracks, "folder_counts": music_engine.folder_counts(idx),
            "suggestions": {n: e for n, e in sugg.items() if n in idx["tracks"]}}


@router.post("/upload", dependencies=[Depends(require_admin)])
async def upload_track(file: UploadFile = File(...),
                       folder: str = Form("")) -> dict:
    """Upload one music file (mp3/wav/flac). Same name -> 409 (never overwrites).

    ``folder`` (form field, optional) = the folder the file belongs to — the
    PERMANENT ownership recorded on the index entry ("" = uncategorised /
    library root). The file itself always lands FLAT at the library root
    (``music_library/<name>``) — folders are index metadata, not subdirectories
    (the bare file name is the global reference key). A non-empty ``folder``
    must already exist -> 404 (same transaction semantics as the 409)."""
    raw_name = file.filename or ""
    try:
        name = music_engine.validate_music_name(raw_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    target_folder = ""
    if folder:
        try:
            target_folder = music_engine.validate_folder_name(folder)
        except ValueError as e:
            raise HTTPException(400, str(e))
    d = _library_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / name
    # Stream to a sidecar temp file with the global size cap (same pattern as
    # /api/files/upload) — a large admin upload must never sit in API-process
    # memory.
    fd, tmp_name = tempfile.mkstemp(prefix=".upload_", suffix=".tmp", dir=str(d))
    tmp = Path(tmp_name)
    size = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "文件超过大小限制")
                handle.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"上传内容为空（{name}）。")

    cfg = get_config()
    ffprobe = cfg.ffmpeg.ffprobe_path

    def _mutate(idx: dict) -> None:
        if target_folder and target_folder not in idx.get("folders", {}):
            raise HTTPException(404, f"文件夹「{target_folder}」不存在。")
        if name in idx["tracks"] or dest.exists():
            raise HTTPException(
                409, f"已存在同名音乐（{name}）——请改名后上传（不会覆盖）。"
            )
        os.replace(tmp, dest)  # moves the staged bytes into place
        dur, _err = probe_duration(dest, ffprobe)
        if not math.isfinite(dur):
            dur = 0.0  # probe failure is non-blocking
        idx["tracks"][name] = {
            "duration": round(dur, 3),
            "enabled": True,
            "description": "",
            "tags": music_engine.empty_track_tags(),
            "added_at": datetime.now().isoformat(timespec="seconds"),
            "folder": target_folder,
        }

    try:
        idx = music_engine.update_index(_mutate)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:
        # A write failure may leave the file on disk without an index entry —
        # remove the orphan so a re-upload of the same name is not blocked.
        for orphan in (dest, tmp):
            try:
                orphan.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(500, f"音乐写入失败：{e}")
    return {"name": name, "track": idx["tracks"][name]}


@router.get("/preview/{name}")
def preview_track(name: str):
    """Stream a music file for in-page preview (the library is NOT served via
    the workspace files API)."""
    p = _track_path(name)
    if not p.is_file():
        raise HTTPException(404, f"音乐库中找不到 {name}")
    return FileResponse(p, media_type=MEDIA_TYPES.get(p.suffix.lower(), "application/octet-stream"))


# --------------------------------------------------------------------------- #
# track edits
# --------------------------------------------------------------------------- #

@router.put("/tracks/{name}", dependencies=[Depends(require_admin)])
def update_track(name: str, body: TrackUpdate) -> dict:
    """Update a track's tags / enabled / description. Out-of-vocabulary tags
    are folded into the custom bucket (engines.music.normalize_track_tags)."""
    p = _track_path(name)
    if not p.is_file():
        raise HTTPException(404, f"音乐库中找不到 {name}")

    def _mutate(idx: dict) -> None:
        tr = idx["tracks"].get(name)
        if not isinstance(tr, dict):
            raise HTTPException(404, f"音乐库中找不到 {name}")
        if body.tags is not None:
            tr["tags"] = music_engine.normalize_track_tags(body.tags, idx["tags"])
        if body.enabled is not None:
            tr["enabled"] = bool(body.enabled)
        if body.description is not None:
            tr["description"] = body.description

    idx = music_engine.update_index(_mutate)
    if body.tags is not None:
        # The user made a tag decision — the AI candidate for this track is
        # consumed (the 弹层「全部采用/确认并修改」both end in this PUT).
        music_engine.clear_suggestion(name)
    return {"name": name, "track": idx["tracks"][name]}


@router.delete("/tracks/{name}", dependencies=[Depends(require_admin)])
def delete_track(name: str) -> dict:
    """Delete one track. Locked chapter references skip it (unlocked ones do
    not block — the chapter degrades to music_missing, re-match self-heals)."""
    deleted, reason = _delete_track(name)
    return {"deleted": [name] if deleted else [],
            "skipped": [] if deleted else [{"name": name, "reason": reason}],
            "missing": []}


@router.post("/tracks/batch-enable", dependencies=[Depends(require_admin)])
def batch_enable(body: BatchNames) -> dict:
    if not body.names:
        raise HTTPException(400, "未选择音乐。")
    known = set(music_engine.load_index()["tracks"])
    missing = [n for n in body.names if n not in known]
    enabled = bool(body.enabled)

    def _mutate(idx: dict) -> None:
        for n in body.names:
            tr = idx["tracks"].get(n)
            if isinstance(tr, dict):
                tr["enabled"] = enabled

    music_engine.update_index(_mutate)
    return {"updated": len(body.names) - len(missing), "enabled": enabled,
            "missing": missing}


@router.post("/tracks/batch-tags", dependencies=[Depends(require_admin)])
def batch_tags(body: BatchTags) -> dict:
    """Add/remove tag names to/from the SELECTED tracks (body.tracks = 选中音乐,
    body.names = 标签名). Out-of-vocabulary tag names are allowed here (the
    registry is not required) — the per-track normalize on a later PUT would
    fold them into custom, so the UI only offers registry names."""
    tracks = [n.strip() for n in body.tracks if isinstance(n, str) and n.strip()]
    if not tracks:
        raise HTTPException(400, "未选择音乐。")
    if body.category not in music_engine.TAG_CATEGORIES:
        raise HTTPException(400, f"未知标签分类：{body.category}")
    if body.op not in ("add", "remove"):
        raise HTTPException(400, f"未知操作：{body.op}")
    names = [n.strip() for n in body.names if isinstance(n, str) and n.strip()]
    if not names:
        raise HTTPException(400, "未提供标签。")
    known = set(music_engine.load_index()["tracks"])
    missing = [n for n in tracks if n not in known]

    def _mutate(idx: dict) -> None:
        for n in tracks:
            tr = idx["tracks"].get(n)
            if not isinstance(tr, dict) or not isinstance(tr.get("tags"), dict):
                continue
            bucket = tr["tags"].setdefault(body.category, [])
            if body.op == "add":
                for t in names:
                    if t not in bucket:
                        bucket.append(t)
            else:
                tr["tags"][body.category] = [t for t in bucket if t not in names]

    music_engine.update_index(_mutate)
    return {"updated": len(tracks) - len(missing), "op": body.op,
            "category": body.category, "names": names, "missing": missing}


@router.post("/tracks/batch-delete", dependencies=[Depends(require_admin)])
def batch_delete(body: BatchNames) -> dict:
    """Delete many tracks with the same locked-reference skip semantics as the
    single delete (shared via _delete_track)."""
    if not body.names:
        raise HTTPException(400, "未选择音乐。")
    known = set(music_engine.load_index()["tracks"])
    missing = [n for n in body.names if n not in known]
    deleted: list[str] = []
    skipped: list[dict] = []
    for n in body.names:
        if n in missing:
            continue
        try:
            ok, reason = _delete_track(n)
        except HTTPException as e:
            # Traversal-shaped names are reported per item, not aborting the batch.
            skipped.append({"name": n, "reason": e.detail})
            continue
        if ok:
            deleted.append(n)
        else:
            skipped.append({"name": n, "reason": reason})
    return {"deleted": deleted, "skipped": skipped, "missing": missing}


@router.post("/tracks/apply-suggestions", dependencies=[Depends(require_admin)])
def apply_suggestions(body: ApplySuggestionsReq) -> dict:
    """Adopt the cached AI tag candidates (``music_tag_suggestions.json``) for
    the given tracks — the「AI 推荐采用」one-click confirmation.

    A track is only touched when it has a candidate WITH tags AND no manual
    tags at all (四类全空) — a user decision is NEVER overwritten (tracks with
    manual tags are reported in ``skipped_manual`` and keep their candidate).
    Applied tracks consume their candidate (same semantics as a user-confirmed
    PUT with tags). Guards: per-name traversal 400 -> empty selection 400;
    missing tracks are reported, not an error (batch-endpoint precedent)."""
    names: list[str] = []
    for n in body.names or []:
        try:
            bare = music_engine.validate_music_name(n)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if bare not in names:
            names.append(bare)
    if not names:
        raise HTTPException(400, "请选择要采用 AI 推荐的音乐。")
    idx = music_engine.load_index()
    sugg = music_engine.load_suggestions().get("tracks") or {}
    apply_map: dict[str, dict] = {}
    skipped_manual: list[str] = []
    no_suggestion: list[str] = []
    missing: list[str] = []
    for n in names:
        tr = idx["tracks"].get(n)
        if not isinstance(tr, dict) or not (_library_dir() / n).is_file():
            missing.append(n)
            continue
        entry = sugg.get(n)
        cand = entry.get("tags") if isinstance(entry, dict) else None
        if not isinstance(cand, dict) or not any(cand.get(c) for c in ("scene", "mood", "emotion")):
            no_suggestion.append(n)
            continue
        if music_engine.track_has_manual_tags(tr):
            # The user already made a tag decision — never overwrite it.
            skipped_manual.append(n)
            continue
        apply_map[n] = cand
    applied: list[str] = []
    if apply_map:
        def _mutate(idx2: dict) -> None:
            # Re-check under the index lock: a manual tag edit made microseconds
            # earlier still wins (the no-overwrite rule holds at write time too).
            for n, cand in apply_map.items():
                tr = idx2["tracks"].get(n)
                if not isinstance(tr, dict) or music_engine.track_has_manual_tags(tr):
                    continue
                tr["tags"] = music_engine.normalize_track_tags(cand, idx2["tags"])
                applied.append(n)

        music_engine.update_index(_mutate)
        for n in applied:
            # The candidate is consumed (same as a user-confirmed PUT with tags).
            music_engine.clear_suggestion(n)
    return {
        "applied": applied,
        "skipped_manual": skipped_manual,
        "no_suggestion": no_suggestion,
        "missing": missing,
    }


# --------------------------------------------------------------------------- #
# folders (index metadata — user organisation; matching never reads them)
# --------------------------------------------------------------------------- #

class FolderCreate(BaseModel):
    name: str


class FolderRename(BaseModel):
    new_name: str


class TrackMove(BaseModel):
    names: list[str]
    folder: str = ""  # "" = move to uncategorised (library root)


@router.post("/folders", dependencies=[Depends(require_admin)])
def create_folder(body: FolderCreate) -> dict:
    """Create a folder (metadata only — no filesystem entity). Duplicate name
    -> 409 (checked under the same index transaction)."""
    try:
        name = music_engine.validate_folder_name(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    def _mutate(idx: dict) -> None:
        if name in idx.get("folders", {}):
            raise HTTPException(409, f"文件夹「{name}」已存在。")
        music_engine.create_folder(idx, name)

    idx = music_engine.update_index(_mutate)
    return {"folder": name, "folders": idx["folders"]}


@router.put("/folders/{folder}", dependencies=[Depends(require_admin)])
def rename_folder(folder: str, body: FolderRename) -> dict:
    """Rename a folder + propagate to every track's ``folder`` field (single
    atomic transaction). 404 source missing / 409 target taken / no-op when
    old == new (tag rename precedent)."""
    old = (folder or "").strip()
    if not old:
        raise HTTPException(400, "文件夹名称不能为空。")
    try:
        new = music_engine.validate_folder_name(body.new_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if old == new:
        return {"folder": new, "renamed_tracks": 0,
                "folders": music_engine.load_index()["folders"]}

    renamed = 0

    def _mutate(idx: dict) -> None:
        nonlocal renamed
        folders = idx.get("folders", {})
        if old not in folders:
            raise HTTPException(404, f"文件夹「{old}」不存在。")
        if new in folders:
            raise HTTPException(409, f"文件夹「{new}」已存在。")
        # The source-existence check above runs under the same lock as the
        # rename, so no concurrent delete can race the transaction.
        renamed = music_engine.apply_folder_rename(idx, old, new)

    idx = music_engine.update_index(_mutate)
    return {"folder": new, "renamed_tracks": renamed,
            "folders": idx["folders"]}


@router.delete("/folders/{folder}", dependencies=[Depends(require_admin)])
def delete_folder(folder: str) -> dict:
    """Delete a folder. NON-EMPTY -> 409 (tracks must be moved out first —
    music files are never deleted and a non-empty folder is never emptied
    silently)."""
    name = (folder or "").strip()
    if not name:
        raise HTTPException(400, "文件夹名称不能为空。")
    idx = music_engine.load_index()
    if name not in idx.get("folders", {}):
        raise HTTPException(404, f"文件夹「{name}」不存在。")

    def _mutate(idx: dict) -> None:
        if name not in idx.get("folders", {}):
            raise HTTPException(404, f"文件夹「{name}」不存在。")
        n = music_engine.folder_counts(idx).get(name, 0)
        if n:
            raise HTTPException(
                409, f"非空文件夹不能删除：请先将其中 {n} 首音乐移到其他文件夹或未分类。"
            )
        music_engine.delete_folder(idx, name)

    idx = music_engine.update_index(_mutate)
    return {"deleted": [name], "folders": idx["folders"]}


@router.post("/tracks/move", dependencies=[Depends(require_admin)])
def move_tracks(body: TrackMove) -> dict:
    """Move tracks to a folder (single or batch — one endpoint; ``folder = ""``
    = uncategorised / library root). Guards: per-name traversal 400 -> empty
    selection 400 -> non-empty target folder missing 404; missing tracks are
    reported, not an error (batch endpoint precedent)."""
    names: list[str] = []
    for n in body.names or []:
        try:
            bare = music_engine.validate_music_name(n)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if bare not in names:
            names.append(bare)
    if not names:
        raise HTTPException(400, "未选择音乐。")
    target = ""
    if body.folder:
        try:
            target = music_engine.validate_folder_name(body.folder)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if target and target not in music_engine.load_index().get("folders", {}):
        raise HTTPException(404, f"文件夹「{target}」不存在。")

    moved: list[str] = []
    missing: list[str] = []

    def _mutate(idx: dict) -> None:
        nonlocal moved, missing
        if target and target not in idx.get("folders", {}):
            raise HTTPException(404, f"文件夹「{target}」不存在。")
        moved, missing = music_engine.apply_track_move(idx, names, target)

    music_engine.update_index(_mutate)
    return {"moved": moved, "missing": missing, "folder": target}


# --------------------------------------------------------------------------- #
# tag management
# --------------------------------------------------------------------------- #

def _rewrite_analysis_tags(chapters, old: str, new: str | None, category: str) -> bool:
    """Rewrite ``old`` → ``new`` in each chapter's ``category`` tag lists
    (``new=None`` removes it). Returns True when any chapter changed."""
    if not isinstance(chapters, dict):
        return False
    changed = False
    for ch in chapters.values():
        if not isinstance(ch, dict):
            continue
        lst = ch.get(category)
        if not isinstance(lst, list):
            continue
        if old in lst:
            ch[category] = [new if v == old else v for v in lst] if new else \
                [v for v in lst if v != old]
            changed = True
    return changed


def _propagate_chapter_analysis(old: str, new: str | None, category: str) -> None:
    """Rewrite a (renamed) tag in the current workspace's own analysis cache
    (``08_bgm/chapter_music_analysis.json``). Best-effort: no workspace / no
    file / corrupt file -> nothing to do. The saved per-chapter assignments
    (``08_bgm/bgm_assignments.json``) are NOT rewritten (they are a
    historical record).

    The read → rewrite → publish cycle holds the cross-process
    ``bgm_storage.storage_lock`` (shared with the Worker's own cache writes)
    and publishes atomically, so a concurrent BGM analysis task cannot lose
    either side's write."""
    from ..core.paths import get_or_prepare_layout
    from ..engines import bgm_storage

    layout = get_or_prepare_layout()
    bgm = layout.bgm
    if bgm is None or not (bgm / bgm_storage.ANALYSIS_NAME).exists():
        return
    with bgm_storage.storage_lock(layout):
        data = bgm_storage.load_analysis(layout)
        if not _rewrite_analysis_tags(data.get("chapters"), old, new, category):
            return
        try:
            bgm_storage.save_analysis(layout, data)
        except OSError:
            pass


@router.post("/tags", dependencies=[Depends(require_admin)])
def create_tag(body: TagCreate) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "标签名不能为空。")
    if body.category not in music_engine.TAG_CATEGORIES:
        raise HTTPException(400, f"未知标签分类：{body.category}")
    idx = music_engine.update_index(lambda i: _add_tag(i, body.category, name))
    return {"tags": idx["tags"]}


def _add_tag(idx: dict, category: str, name: str) -> None:
    """Registry add with the global-uniqueness rule (409 on any-bucket clash)."""
    if name in music_engine.all_tag_names(idx["tags"]):
        raise HTTPException(409, f"标签「{name}」已存在。")
    idx["tags"].setdefault(category, []).append(name)


@router.post("/tags/rename", dependencies=[Depends(require_admin)])
def rename_tag(body: TagRename) -> dict:
    old = (body.name or "").strip()
    new = (body.new_name or "").strip()
    if not old or not new:
        raise HTTPException(400, "标签名不能为空。")
    if body.category not in music_engine.TAG_CATEGORIES:
        raise HTTPException(400, f"未知标签分类：{body.category}")
    if old == new:
        return {"tags": music_engine.load_index()["tags"]}
    affected = music_engine.update_index(
        lambda idx: _rename_tag(idx, body.category, old, new)
    )
    _propagate_chapter_analysis(old, new, body.category)
    return {"tags": affected["tags"], "affected_tracks": _affected_count(affected, body.category, old, new)}


def _rename_tag(idx: dict, category: str, old: str, new: str) -> None:
    bucket = idx["tags"].get(category)
    if not isinstance(bucket, list) or old not in bucket:
        raise HTTPException(404, f"标签「{old}」不在 {category} 分类中。")
    if new in music_engine.all_tag_names(idx["tags"]):
        raise HTTPException(409, f"标签「{new}」已存在。")
    music_engine.apply_tag_rename(idx, category, old, new)


def _affected_count(idx: dict, category: str, old: str, new: str) -> int:
    # Recompute the affected-track count from the post-rename index.
    n = 0
    for tr in idx.get("tracks", {}).values():
        if isinstance(tr, dict) and isinstance(tr.get("tags"), dict):
            if new in (tr["tags"].get(category) or []):
                n += 1
    return n


@router.delete("/tags/{category}/{name}", dependencies=[Depends(require_admin)])
def delete_tag(category: str, name: str) -> dict:
    if category not in music_engine.TAG_CATEGORIES:
        raise HTTPException(400, f"未知标签分类：{category}")
    idx = music_engine.update_index(lambda i: _delete_tag(i, category, name))
    _propagate_chapter_analysis(name, None, category)
    return {"tags": idx["tags"], "deleted_track_refs": _affected_count(idx, category, name, None)}


def _delete_tag(idx: dict, category: str, name: str) -> None:
    bucket = idx["tags"].get(category)
    if not isinstance(bucket, list) or name not in bucket:
        raise HTTPException(404, f"标签「{name}」不在 {category} 分类中。")
    music_engine.apply_tag_delete(idx, category, name)


# --------------------------------------------------------------------------- #
# AI tag recommendation (durable Worker task)
# --------------------------------------------------------------------------- #

@router.post("/suggest-tags", dependencies=[Depends(require_admin)])
def suggest_tags_durable(
    body: SuggestTagsReq,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Submit the single-track recommendation as a durable Worker task."""
    p = _track_path(body.name)
    if not p.is_file():
        raise HTTPException(404, f"音乐库中找不到{body.name}")
    cfg = get_config()
    if not cfg.llm.model_name:
        raise HTTPException(400, "尚未配置 LLM 模型（设置 → LLM → model_name）。")
    if body.name in active_durable_targets(
        task_type="music.suggest_tags", payload_key="name", ctx=ctx, db=db,
    ):
        raise HTTPException(409, "该音乐已有 AI 推荐任务在途")
    task = submit_legacy_engine_task(
        task_type="music.suggest_tags",
        label=f"{AI_TAGS_LABEL}：{body.name}",
        payload={
            "name": body.name,
            "description": body.description,
            "config": cfg.model_dump(mode="json"),
        },
        ctx=ctx,
        db=db,
        idempotency_prefix=f"music-ai-tags-single:{body.name}",
    )
    return {"task_id": task["id"]}


# --------------------------------------------------------------------------- #
# AI tag recognition — batch (one Task per selected track, shared LLM gate)
# --------------------------------------------------------------------------- #

class SuggestBatchReq(BaseModel):
    names: list[str]




@router.post("/suggest-tags-batch", dependencies=[Depends(require_admin)])
def suggest_tags_batch(
    body: SuggestBatchReq,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Start one AI-tag Task per selected track (PENDING shells + the shared
    BGM coordinator; gate = the process-wide LLM gate ``gate()``; slot scope =
    the whole task).

    On success the worker AUTO-ADOPTS the candidate into the index when the
    track has no manual tags yet (2026-09：未手动打标的条目直接生效) and
    consumes it; tracks tagged meanwhile keep their candidate in
    ``music_tag_suggestions.json`` for the「AI 推荐采用」button / tag editor
    (manual decisions are never overwritten).

    Guards: per-name (traversal 400 / missing 404) → empty selection 400 →
    model not configured 400 → same-track in-flight 409 (non-conflicting
    tracks allowed). Returns ``{"task_ids": [...], "tracks": [{name, task_id}]}``.
    """
    names: list[str] = []
    for n in body.names or []:
        try:
            bare = music_engine.validate_music_name(n)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not (_library_dir() / bare).is_file():
            raise HTTPException(404, f"音乐库中找不到 {bare}")
        if bare not in names:
            names.append(bare)
    if not names:
        raise HTTPException(400, "请选择要 AI 识别标签的音乐。")
    cfg = get_config()
    if not cfg.llm.model_name:
        raise HTTPException(400, "尚未配置 LLM 模型（设置 → LLM → model_name）。")
    active_names = active_durable_targets(
        task_type="music.suggest_tags", payload_key="name", ctx=ctx, db=db,
    )
    conflicts = [name for name in names if name in active_names]
    if conflicts:
        raise HTTPException(409, "以下音乐已有 AI 推荐任务在途：" + "、".join(conflicts))
    created = []
    for name in names:
        task = submit_legacy_engine_task(
            task_type="music.suggest_tags",
            label=f"{AI_TAGS_LABEL}：{name}",
            payload={
                "name": name,
                "config": cfg.model_dump(mode="json"),
            },
            ctx=ctx,
            db=db,
            idempotency_prefix=f"music-ai-tags:{name}",
        )
        created.append({"name": name, "task_id": task["id"]})
    return {"task_ids": [item["task_id"] for item in created], "tracks": created}
