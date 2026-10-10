"""Managed-file boundaries shared by inventory and existing workspace traversal."""
from __future__ import annotations

import os
from pathlib import Path


def is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def safe_regular_path(root: Path, relative: str, *, directory: bool = False) -> Path:
    """Resolve every component without crossing a symlink, junction or root."""
    if not relative or "\\" in relative or "\x00" in relative:
        raise ValueError("非法资源路径")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ValueError("非法资源路径")
    if is_link_or_junction(root):
        raise ValueError("资源目录不可安全访问")
    resolved_root = root.resolve(strict=True)
    current = root
    for part in parts:
        current = current / part
        if is_link_or_junction(current):
            raise ValueError("链接资源不可访问")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("资源路径越界")
    if not (resolved.is_dir() if directory else resolved.is_file()):
        raise FileNotFoundError(relative)
    return resolved


def file_identity(stat: os.stat_result) -> tuple[int, ...]:
    # POSIX st_dev is not stable across reboots (NVMe/partition minor numbers
    # are reassigned by enumeration order), which silently invalidated every
    # stored identity -- deliveries, input versions -- after a restart. The
    # inode already pins the file within its filesystem, so the device slot is
    # fixed at 0 there. Windows keeps the volume serial (stable).
    device = stat.st_dev if os.name == "nt" else 0
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, device, stat.st_ino)


def identity_matches(current, recorded) -> bool:
    """Compare a fresh identity with a persisted one.

    Identities persisted before ``file_identity`` dropped the POSIX device
    number carry a real (reboot-unstable) st_dev in slot 3; on POSIX that slot
    is ignored so those records keep matching. Windows compares everything.
    """
    if current is None or recorded is None:
        return current is recorded
    current, recorded = list(current), list(recorded)
    if os.name != "nt" and len(current) == len(recorded) == 5:
        return current[:3] == recorded[:3] and current[4] == recorded[4]
    return current == recorded


def matches_open_file_identity(stat: os.stat_result, expected: tuple[int, ...]) -> bool:
    actual = file_identity(stat)
    # Windows path stat and handle fstat can return different creation times
    # for the same freshly written NTFS file. Retain size, write time, volume
    # and file id here; callers still compare the full path identity before
    # and after reading. POSIX change time remains part of the handle check.
    indices = (0, 1, 3, 4) if os.name == "nt" else (0, 1, 2, 3, 4)
    return all(actual[index] == expected[index] for index in indices)
