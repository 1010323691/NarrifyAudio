"""Owned process trees. Windows children enter a Job before their first instruction."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import uuid
from pathlib import Path

import psutil


def process_identity(pid: int) -> dict:
    try:
        process = psutil.Process(pid)
        return {"pid": pid, "created": process.create_time()}
    except psutil.NoSuchProcess:
        return {}


def identity_alive(identity: dict) -> bool:
    if not identity:
        return False
    try:
        process = psutil.Process(identity["pid"])
        return abs(process.create_time() - identity["created"]) < 0.01 and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    # AccessDenied is deliberately propagated: unknown is not proof of exit.


def windows_job_alive(name: str) -> bool:
    """Inspect a named owned Job after its original Worker has exited."""
    from ctypes import wintypes as w
    if not name.startswith("Local\\NarrifyAudio-"):
        raise ValueError("Unknown owned Job identity")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
    kernel.OpenJobObjectW.restype = w.HANDLE
    kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    handle = kernel.OpenJobObjectW(0x4, False, name)
    if not handle:
        error = ctypes.get_last_error()
        if error == 2:
            return False
        raise ctypes.WinError(error)
    try:
        class Accounting(ctypes.Structure):
            _fields_ = [(key, ctypes.c_int64) for key in ("user", "kernel", "period_user", "period_kernel")] + [
                (key, w.DWORD) for key in ("faults", "total", "active", "terminated")]
        info = Accounting()
        if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active > 0
    finally:
        kernel.CloseHandle(handle)


class WindowsJob:
    def __init__(self):
        from ctypes import wintypes as w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]

        class Limits(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("limits", Limits), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.name = "Local\\NarrifyAudio-" + uuid.uuid4().hex
        self.handle = self.kernel.CreateJobObjectW(None, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, proc):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def empty(self) -> bool:
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, ctypes.c_uint32) for name in ("faults", "total", "active", "terminated")]
        info = Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active == 0

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def spawn_owned(cmd: list[str] | str, **kwargs) -> subprocess.Popen:
    job = WindowsJob() if os.name == "nt" else None
    proc = None
    try:
        if job:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            kwargs.update(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x4, startupinfo=startup)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kwargs)
        proc._narrify_job = job
        if job:
            job.assign(proc)
            ntdll = ctypes.WinDLL("ntdll")
            ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
            ntdll.NtResumeProcess.restype = ctypes.c_long
            if ntdll.NtResumeProcess(int(proc._handle)) != 0:
                raise RuntimeError("Unable to resume owned process")
        return proc
    except BaseException:
        if proc:
            proc.kill()
            proc.wait()
        if job:
            job.close()
        raise


def tree_exited(proc) -> bool:
    job = getattr(proc, "_narrify_job", None)
    if job and job.handle:
        return job.empty()
    if proc.poll() is None:
        return False
    if os.name != "nt":
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return True
        # Orphan zombies no longer own GPU memory; init may reap them later.
        for process in psutil.process_iter():
            try:
                if os.getpgid(process.pid) == proc.pid and process.status() != psutil.STATUS_ZOMBIE:
                    return False
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
        # Permission errors propagate: unknown is not proof of exit.
    return True


def terminate_owned(proc) -> None:
    """Only for cancellation, failed startup, or abnormal worker cleanup."""
    job = getattr(proc, "_narrify_job", None)
    if job and job.handle:
        job.terminate()
    elif os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.poll() is None:
        proc.kill()


def close_owned(proc) -> None:
    job = getattr(proc, "_narrify_job", None)
    if job and job.handle:
        if not job.empty():
            raise RuntimeError("Owned process tree has not exited")
        job.close()


def script_command(path: str) -> list[str] | str:
    suffix = Path(path).suffix.lower()
    if suffix == ".sh":
        # Explicit Bash argv preserves spaces and Unicode without shell interpolation.
        return ["/bin/bash", "--", path]
    if suffix == ".ps1":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", path]
    if suffix not in {".cmd", ".bat"}:
        raise ValueError("不支持的 LLM 脚本类型")
    # cmd requires a command string; reject shell metacharacters even in quoted paths.
    if any(character in path for character in '\r\n"&|<>^%!'):
        raise ValueError("脚本路径包含不支持的命令字符")
    # cmd parses quotes differently from the C argv convention used by list2cmdline.
    # A raw command line keeps the outer /s quotes and the inner filename quotes intact.
    executable = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe")])
    return executable + ' /d /s /c ""' + path + '""'


def validate_script_platform(path: str) -> None:
    supported = {".ps1", ".cmd", ".bat"} if os.name == "nt" else {".sh"}
    if Path(path).suffix.lower() not in supported:
        raise ValueError("当前操作系统仅支持 " + "、".join(sorted(supported)) + " LLM 脚本")
    if os.name != "nt" and not Path("/bin/bash").is_file():
        raise ValueError("运行 .sh 脚本需要 /bin/bash")
