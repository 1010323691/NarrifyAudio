"""Safe, backend-owned filesystem browsing and folder management."""
from __future__ import annotations

import ctypes
import os
import shutil
import string
from dataclasses import dataclass
from pathlib import Path

from . import config as core_config
from . import paths as core_paths


class FilesystemError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Drive:
    path: str
    name: str
    kind: str


def normalize_path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value).strip()
    if not raw:
        raise FilesystemError(400, "路径不能为空")
    path = Path(os.path.abspath(os.path.normpath(os.path.expanduser(raw))))
    if not path.is_absolute():
        raise FilesystemError(400, "必须使用绝对路径")
    return path


def io_path(path: Path) -> Path:
    """Use the Windows extended-length prefix only for filesystem syscalls."""
    if os.name != "nt":
        return path
    raw = str(path)
    if raw.startswith("\\\\?\\") or len(raw) < 248:
        return path
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


def _same_or_child(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(parent))]) == os.path.normcase(str(parent))
    except ValueError:
        return False


def _system_roots() -> list[Path]:
    if os.name != "nt":
        return [Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev")]
    values = [os.environ.get("WINDIR"), os.environ.get("SystemRoot"), os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramData")]
    roots = [Path(value) for value in values if value]
    for drive in string.ascii_uppercase:
        roots.extend(Path(f"{drive}:\\{name}") for name in ("$Recycle.Bin", "System Volume Information", "Recovery") if Path(f"{drive}:\\").exists())
    return roots


def protected_reason(path: Path, operation: str) -> str | None:
    path = Path(os.path.abspath(os.path.normpath(str(path))))
    if path.parent == path or (path.anchor and os.path.normcase(str(path)) == os.path.normcase(path.anchor)):
        return "磁盘根目录不可操作"
    if io_path(path).is_symlink() and operation in {"rename", "delete"}:
        return "符号链接或目录联接不可修改"
    for root in _system_roots():
        if _same_or_child(path, root):
            return "系统目录不可操作"
    project_root = Path(core_paths.PROJECT_ROOT)
    if _same_or_child(path, project_root):
        return "应用目录不可操作"
    if operation in {"rename", "delete"} and _same_or_child(project_root, path):
        return "应用目录或其祖先不可操作"
    active = core_config._workspace_path()
    if operation in {"rename", "delete"}:
        if active is not None and _same_or_child(active, path):
            return "当前工作空间或其祖先不可操作"
        if active is not None:
            protected_children = (*core_paths.WORKSPACE_DIR_NAMES, "logs", "config")
            if any(_same_or_child(path, active / name) for name in protected_children):
                return "工作空间系统目录不可操作"
    return None


def _raise_oserror(exc: OSError, action: str) -> None:
    if isinstance(exc, PermissionError):
        raise FilesystemError(403, f"没有权限{action}此目录") from exc
    if isinstance(exc, FileNotFoundError):
        raise FilesystemError(404, "目录不存在或磁盘不可用") from exc
    if isinstance(exc, FileExistsError):
        raise FilesystemError(409, "同名文件夹已存在") from exc
    if isinstance(exc, (OSError,)):  # busy, disconnected network drive, sharing violation
        raise FilesystemError(409, f"无法{action}此目录，目录可能被其他程序占用或磁盘已失效") from exc


def list_drives() -> list[dict[str, str]]:
    drives: list[Drive] = []
    if os.name == "nt":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        labels = {2: "可移动磁盘", 3: "本地磁盘", 4: "网络磁盘", 5: "光盘"}
        for index, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << index):
                root = f"{letter}:\\"
                kind = labels.get(get_type(root), "磁盘")
                drives.append(Drive(root, f"{letter}:", kind))
    else:
        mounts = {"/"}
        try:
            for line in Path("/proc/self/mounts").read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) > 1 and parts[1].startswith("/"):
                    mounts.add(parts[1].replace("\\040", " "))
        except OSError:
            pass
        for mount in sorted(mounts):
            path = Path(mount)
            if path.exists() and path.is_dir():
                drives.append(Drive(str(path), path.name or "/", "挂载点"))
    return [{"path": item.path, "name": item.name, "kind": item.kind} for item in drives]


def list_directories(value: str) -> dict:
    path = normalize_path(value)
    native_path = io_path(path)
    if not native_path.exists():
        raise FilesystemError(404, "目录不存在或磁盘不可用")
    if not native_path.is_dir():
        raise FilesystemError(400, "只能浏览文件夹")
    try:
        entries = []
        for entry in native_path.iterdir():
            try:
                if not entry.is_dir():
                    continue
                child = path / entry.name
                entries.append({
                    "name": entry.name,
                    "path": str(child),
                    "can_rename": protected_reason(child, "rename") is None,
                    "can_delete": protected_reason(child, "delete") is None,
                    "protected_reason": protected_reason(child, "delete"),
                })
            except OSError:
                continue
        entries.sort(key=lambda item: item["name"].casefold())
    except OSError as exc:
        _raise_oserror(exc, "读取")
    parent = path.parent if path.parent != path else None
    return {
        "path": str(path),
        "parent_path": str(parent) if parent else None,
        "can_create": protected_reason(path, "create") is None,
        "folders": entries,
    }


def validate_folder_name(name: str) -> str:
    value = name or ""
    if not value.strip() or value in {".", ".."}:
        raise FilesystemError(400, "文件夹名称不能为空或为特殊目录名")
    if "\x00" in value or "/" in value or "\\" in value:
        raise FilesystemError(400, "文件夹名称不能包含路径分隔符")
    if os.name == "nt":
        if any(char in value for char in '<>:"|?*') or value[-1] in {".", " "}:
            raise FilesystemError(400, "文件夹名称包含 Windows 不允许的字符")
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if value.split(".", 1)[0].upper() in reserved:
            raise FilesystemError(400, "文件夹名称是 Windows 保留设备名")
    return value


def create_folder(parent_value: str, name: str) -> dict:
    parent = normalize_path(parent_value)
    name = validate_folder_name(name)
    reason = protected_reason(parent, "create")
    if reason:
        raise FilesystemError(403, reason)
    if not io_path(parent).exists() or not io_path(parent).is_dir():
        raise FilesystemError(404, "父目录不存在或磁盘不可用")
    target = parent / name
    if io_path(target).exists():
        raise FilesystemError(409, "同名文件夹已存在")
    try:
        io_path(target).mkdir()
    except OSError as exc:
        _raise_oserror(exc, "创建")
    return {"path": str(target), "name": target.name}


def rename_folder(path_value: str, new_name: str) -> dict:
    source = normalize_path(path_value)
    name = validate_folder_name(new_name)
    reason = protected_reason(source, "rename")
    if reason:
        raise FilesystemError(403, reason)
    if not io_path(source).exists() or not io_path(source).is_dir():
        raise FilesystemError(404, "目录不存在或磁盘不可用")
    target = source.parent / name
    if os.path.normcase(str(target)) != os.path.normcase(str(source)) and io_path(target).exists():
        raise FilesystemError(409, "同名文件夹已存在")
    try:
        io_path(source).rename(io_path(target))
    except OSError as exc:
        _raise_oserror(exc, "重命名")
    return {"path": str(target), "name": target.name}


def delete_folder(path_value: str, recursive: bool, confirmed: bool) -> dict:
    target = normalize_path(path_value)
    reason = protected_reason(target, "delete")
    if reason:
        raise FilesystemError(403, reason)
    if not io_path(target).exists() or not io_path(target).is_dir():
        raise FilesystemError(404, "目录不存在或磁盘不可用")
    if not confirmed:
        raise FilesystemError(400, "删除目录必须经过二次确认")
    try:
        if recursive:
            shutil.rmtree(io_path(target))
        else:
            io_path(target).rmdir()
    except OSError as exc:
        _raise_oserror(exc, "删除")
    return {"deleted": True, "path": str(target)}
