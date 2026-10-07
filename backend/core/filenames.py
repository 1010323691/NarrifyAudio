"""Portable artifact names; legacy spellings are read aliases, never identities."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_DEVICES = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
_LEGACY = re.compile(r"[^\w.()\- ]+", re.UNICODE)
MAX_NAME_CHARS = 180
MAX_NAME_BYTES = 240


def _prefix(value: str, chars: int, byte_count: int) -> str:
    return value[:max(0, chars)].encode("utf-8")[:max(0, byte_count)].decode("utf-8", errors="ignore")


def fit_filename(name: str, *, disambiguator: str = "") -> str:
    """Budget the stem without losing its extension or a collision suffix."""
    extension = Path(name).suffix
    # Path.suffix can itself be an arbitrarily long user string.
    if len(extension) > 32 or len(extension.encode("utf-8")) > 64:
        extension = ""
    stem = name[:-len(extension)] if extension else name
    tail = disambiguator + extension
    chars = MAX_NAME_CHARS - len(tail)
    byte_count = MAX_NAME_BYTES - len(tail.encode("utf-8"))
    shortened = _prefix(stem, chars, byte_count)
    if shortened != stem:
        digest = "~" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
        shortened = _prefix(stem, chars - len(digest), byte_count - len(digest)) + digest
    return (shortened.rstrip(" .") + tail).rstrip(" .")


def safe_filename(name: str, *, fallback: str = "upload.bin") -> str:
    """Sanitize one component while retaining valid Unicode punctuation."""
    candidate = _FORBIDDEN.sub("_", name).strip(" .") or fallback
    if _DEVICES.match(candidate):
        candidate = "_" + candidate
    return fit_filename(candidate)


def unique_filename(name: str, occupied: set[str]) -> str:
    """Disambiguate final names, including Windows case-insensitive extraction."""
    name = safe_filename(name)
    folded = {value.casefold() for value in occupied}
    candidate, number = name, 2
    while candidate.casefold() in folded:
        candidate = fit_filename(name, disambiguator=f" ({number})")
        number += 1
    return candidate


def legacy_storage_name(name: str) -> str:
    """Exact pre-v2 storage spelling, for existing paths only."""
    candidate = _LEGACY.sub("_", Path(name or "upload.bin").name).strip(" .")
    return candidate[:180].rstrip(" .") or "upload.bin"


def filename_aliases(name: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((name, safe_filename(name), legacy_storage_name(name))))


def package_stem(package: str) -> str:
    # Budget for either supported merge extension; keep dotted package stems.
    stem = _FORBIDDEN.sub("_", package).strip(" .") or "audiobook"
    return safe_filename(f"{stem}.mp3")[:-4]


def legacy_package_stem(package: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", package).strip() or "audiobook"


def package_aliases(package: str) -> tuple[str, ...]:
    legacy = legacy_package_stem(package)
    # A NUL spelling could never have been a filesystem path.
    return tuple(dict.fromkeys((package_stem(package), *([legacy] if "\x00" not in legacy else []))))


def workspace_audio_identity(task_type: str, payload: dict) -> str | None:
    key = {"tts.merge": "package", "bgm.mix": "stem", "bgm.segment": "stem"}.get(task_type)
    subject = payload.get(key) if key else None
    if task_type == "bgm.match":
        chapters = payload.get("chapters")
        subject = chapters[0] if isinstance(chapters, list) and len(chapters) == 1 else None
    if not isinstance(subject, str) or not subject.strip():
        return None
    filename = f"{package_stem(subject)}.mp3" if task_type == "tts.merge" else f"{subject}.mp3"
    return filename.casefold()
