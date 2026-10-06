"""A small cross-process advisory lock for shared JSON read-modify-write cycles."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def exclusive_file_lock(path: Path, *, timeout: float = 30.0):
    with _file_lock(path, shared=False, timeout=timeout):
        yield


@contextmanager
def shared_file_lock(path: Path, *, timeout: float = 30.0):
    """Allow concurrent readers while excluding holders of exclusive_file_lock."""
    with _file_lock(path, shared=True, timeout=timeout):
        yield


@contextmanager
def _file_lock(path: Path, *, shared: bool, timeout: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with path.open("a+b") as stream:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.LockFileEx.restype = wintypes.BOOL
            kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                            wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.UnlockFileEx.restype = wintypes.BOOL
            handle = msvcrt.get_osfhandle(stream.fileno())
            overlapped = Overlapped()
            # FAIL_IMMEDIATELY, plus EXCLUSIVE_LOCK for writers. Locking beyond
            # EOF is supported, so no write to the locked byte is necessary.
            flags = 1 if shared else 3
            while True:
                if kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
                    break
                error = ctypes.get_last_error()
                if error != 33:  # ERROR_LOCK_VIOLATION
                    raise ctypes.WinError(error)
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for lock: {path}")
                time.sleep(0.05)
            try:
                yield
            finally:
                if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl

            while True:
                try:
                    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                    fcntl.flock(stream.fileno(), mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for lock: {path}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
