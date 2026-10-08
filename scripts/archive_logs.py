"""Archive closed project logs without touching production outputs or recovery files.

Preview: .venv/bin/python scripts/archive_logs.py --username testuser001
Apply:   .venv/bin/python scripts/archive_logs.py --username testuser001 --apply
Read an archive with gzip/zcat; gzip -dk FILE.log.gz restores its original bytes.
Only plain log files directly inside workspace/<user>/<project>/logs are visited.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Callable

import psutil

LOG_NAME = re.compile(r".+\.log(?:\.\d+)?\Z")
APP_MODULES = {"backend.main", "backend.worker", "backend.worker_pool"}
BLOCK_SIZE = 1024 * 1024


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def allocated_bytes(info: os.stat_result) -> int:
    return info.st_blocks * 512 if hasattr(info, "st_blocks") else info.st_size


def sync_directory(path: Path) -> None:
    if hasattr(os, "O_DIRECTORY"):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(path))


def open_logs() -> set[str]:
    """Inspect accessible processes, refusing to guess about an unreadable Worker."""
    busy = set()
    for process in psutil.process_iter():
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                continue
            if (hasattr(os, "geteuid") and os.geteuid() != 0
                    and process.uids().effective != os.geteuid()):
                continue
            for opened in process.open_files():
                busy.add(path_key(opened.path))
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            try:
                relevant = bool(APP_MODULES.intersection(process.cmdline()))
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                relevant = process.name().lower().startswith(("python", "uvicorn", "narrify"))
            if relevant:
                raise RuntimeError(f"无法检查 Narrify 进程 {process.pid} 的文件，停止归档") from None
    return busy


def log_directories(root: Path, username: str | None, project: str | None):
    for value in (username, project):
        if value and (value in {".", ".."} or any(c in value for c in "/\\\0")):
            raise ValueError("用户名和项目名必须是单个目录名称")
    if linked(root) or not root.is_dir():
        raise ValueError("工作空间根目录不存在或是链接")
    users = [root / username] if username else sorted(root.iterdir())
    for user in users:
        if linked(user) or not user.is_dir():
            continue
        projects = [user / project] if project else sorted(user.iterdir())
        for folder in projects:
            directory = folder / "logs"
            if not linked(folder) and folder.is_dir() and not linked(directory) and directory.is_dir():
                yield directory


@dataclass(frozen=True)
class Candidate:
    path: Path
    root: Path
    file_id: tuple[int, ...]
    allocated: int

    @property
    def size(self) -> int:
        return self.file_id[2]

    def unchanged(self) -> bool:
        try:
            for component in [self.path, *self.path.parents]:
                if linked(component):
                    return False
                if component == self.root:
                    break
            info = self.path.lstat()
            return stat.S_ISREG(info.st_mode) and identity(info) == self.file_id
        except OSError:
            return False


def candidates(root: Path, directories, *, cutoff: float, min_bytes: int, busy: set[str]):
    for directory in directories:
        for path in sorted(directory.iterdir()):
            # app.log is the live rotating logger; archived logs, recovery
            # metadata, directories, symlinks and audio are never candidates.
            if not LOG_NAME.fullmatch(path.name) or path.name == "app.log" or linked(path):
                continue
            info = path.lstat()
            item = Candidate(path, root, identity(info), allocated_bytes(info))
            if (stat.S_ISREG(info.st_mode) and info.st_size >= min_bytes
                    and info.st_mtime <= cutoff and path_key(path) not in busy and item.unchanged()):
                yield item


def archive(item: Candidate, busy_files: Callable[[], set[str]] = open_logs) -> tuple[int, str]:
    """Return actual bytes saved. A verified archive must precede source deletion."""
    source = item.path
    target = source.with_name(source.name + ".gz")
    if not item.unchanged() or path_key(source) in busy_files():
        return 0, "文件在使用或已变化"
    if target.exists() or linked(target):
        return 0, "归档已存在，保留原文件"
    temporary = None
    installed = False
    archive_id = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        digest = hashlib.sha256()
        with os.fdopen(os.open(source, flags), "rb") as original:
            # Handle and path creation times differ on Windows; compare file
            # identity, bytes and last-write time, then recheck the full path.
            opened = identity(os.fstat(original.fileno()))
            if opened[:4] != item.file_id[:4] or not item.unchanged():
                return 0, "文件已变化"
            with tempfile.NamedTemporaryFile(dir=source.parent, prefix=".archive-log-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
                    while block := original.read(BLOCK_SIZE):
                        digest.update(block)
                        compressed.write(block)
                output.flush()
                os.fsync(output.fileno())
        verified = hashlib.sha256()
        with gzip.open(temporary, "rb") as compressed:
            while block := compressed.read(BLOCK_SIZE):
                verified.update(block)
        if digest.digest() != verified.digest():
            raise OSError("归档校验失败，原日志保留")
        if not item.unchanged() or path_key(source) in busy_files():
            return 0, "压缩期间文件被打开或变化"
        compressed_info = temporary.stat()
        compressed_size = compressed_info.st_size
        saved = item.allocated - allocated_bytes(compressed_info)
        if compressed_size >= item.size or saved <= 0:
            return 0, "压缩后没有节省磁盘空间"
        archive_id = identity(compressed_info)
        # Same-directory hard link installs the verified archive atomically
        # without overwriting an archive created by another invocation.
        os.link(temporary, target)
        installed = True
        sync_directory(source.parent)
        if not item.unchanged() or path_key(source) in busy_files():
            return 0, "归档安装期间文件被打开或变化"
        source.unlink()
        installed = False  # successful archive must survive finally
        return saved, "已压缩并校验"
    finally:
        if installed:
            try:
                if not linked(target) and identity(target.stat())[:2] == archive_id[:2]:
                    target.unlink()
            except FileNotFoundError:
                pass
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="压缩已关闭的旧日志；默认只预览，--apply 才执行。")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--root", type=Path, help="工作空间根目录，默认仓库下 workspace")
    scope.add_argument("--log-dir", type=Path, help="明确指定一个日志目录，仅处理该目录的普通日志")
    parser.add_argument("--username", help="仅处理指定账号")
    parser.add_argument("--project", help="仅处理指定项目")
    parser.add_argument("--min-age-hours", type=float, default=1, help="最小文件年龄，默认 1 小时")
    parser.add_argument("--min-bytes", type=int, default=1024, help="最小文件大小，默认 1024 字节")
    parser.add_argument("--apply", action="store_true", help="执行归档，校验成功后删除对应原日志")
    args = parser.parse_args(argv)
    if args.min_age_hours < 0 or args.min_bytes < 1:
        parser.error("文件年龄不能为负，最小文件大小必须大于零")
    if args.log_dir and (args.username or args.project):
        parser.error("--log-dir 不与账号或项目筛选混用")
    try:
        root = (args.log_dir or args.root or Path(__file__).resolve().parents[1] / "workspace").absolute()
        if args.log_dir:
            if linked(root) or not root.is_dir():
                raise ValueError("日志目录不存在或是链接")
            directories = [root]
        else:
            directories = list(log_directories(root, args.username, args.project))
        items = list(candidates(root, directories, cutoff=time.time() - args.min_age_hours * 3600,
                                min_bytes=args.min_bytes, busy=open_logs()))
        print(f"{'执行' if args.apply else '预览'}：{len(items)} 个旧日志，原大小合计 {sum(i.size for i in items):,} 字节")
        saved = 0
        errors = 0
        for item in items:
            if not args.apply:
                print(f"候选 {item.size:>10,} 字节  {item.path}")
                continue
            try:
                amount, message = archive(item)
                saved += amount
                print(f"{message}；节省 {amount:,} 字节  {item.path.name}")
            except (OSError, RuntimeError) as exc:
                errors += 1
                print(f"未处理 {item.path.name}：{exc}")
        print(f"实际释放 {saved:,} 字节。未操作音频、恢复记录、备份或数据库。")
        return 1 if errors else 0
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"无法安全归档：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
