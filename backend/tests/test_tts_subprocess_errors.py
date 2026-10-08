"""GPU failures stay in the run transcript and retain retry metadata."""
import io
import json
from types import SimpleNamespace

import pytest

from backend.engines import tts


class Handle:
    def __init__(self):
        self.logs = []

    def check(self):
        pass

    def log(self, message, level="INFO"):
        self.logs.append(message)


@pytest.mark.parametrize("structured", [False, True])
def test_oom_is_detected_even_when_stderr_tail_loses_error(tmp_path, monkeypatch, structured):
    event = json.dumps({"stage": "batch", "event": "start", "rows": 224})
    output = f"[perf] {event}\n"
    if structured:
        output += '[perf] {"stage":"batch","event":"error","oom":true,"error":"GPU details"}\n'
    error = "Traceback details\n"
    if not structured:
        error += "torch.OutOfMemoryError: CUDA out of memory. private GPU diagnostics\n"
    error += "diagnostic stack frame\n" * 45
    proc = SimpleNamespace(stdout=io.BytesIO(output.encode()), stderr=io.BytesIO(error.encode()),
                           returncode=1, poll=lambda: 1)
    monkeypatch.setattr(tts.GPUServiceManager, "spawn_tts", lambda *a, **kw: proc)
    monkeypatch.setattr(tts.GPUServiceManager, "finish_tts", lambda *a: None)
    log = tmp_path / "run.log"
    handle = Handle()
    with pytest.raises(tts.WorkerOutOfMemory) as caught:
        tts._run_tts_subprocess_once(["python"], handle, lambda line: None,
                                     private_errors=True, log_file=log, log_line=lambda line: None)
    assert caught.value.rows == 224
    assert handle.logs == []
    assert "diagnostic stack frame" in log.read_text("utf-8")
    assert "GPU" in log.read_text("utf-8")
    assert "GPU" not in str(caught.value)


def test_non_oom_failure_also_hides_stderr(tmp_path, monkeypatch):
    proc = SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO(b"private traceback\n"),
                           returncode=1, poll=lambda: 1)
    monkeypatch.setattr(tts.GPUServiceManager, "spawn_tts", lambda *a, **kw: proc)
    monkeypatch.setattr(tts.GPUServiceManager, "finish_tts", lambda *a: None)
    handle = Handle()
    log = tmp_path / "run.log"
    with pytest.raises(RuntimeError) as caught:
        tts._run_tts_subprocess_once(["python"], handle, lambda line: None,
                                     private_errors=True, log_file=log)
    assert "private traceback" not in str(caught.value)
    assert "private traceback" in log.read_text("utf-8")
    assert handle.logs == []


def test_structured_failure_is_retained_when_stderr_is_empty(tmp_path, monkeypatch):
    output = b'[perf] {"stage":"batch","event":"error","oom":true,"error":"CUDA allocation failed"}\n'
    proc = SimpleNamespace(stdout=io.BytesIO(output), stderr=io.BytesIO(), returncode=1, poll=lambda: 1)
    monkeypatch.setattr(tts.GPUServiceManager, "spawn_tts", lambda *a, **kw: proc)
    monkeypatch.setattr(tts.GPUServiceManager, "finish_tts", lambda *a: None)
    log = tmp_path / "run.log"
    with pytest.raises(tts.WorkerOutOfMemory):
        tts._run_tts_subprocess_once(["python"], Handle(), lambda line: None,
                                     private_errors=True, log_file=log, log_line=lambda line: None)
    assert "CUDA allocation failed" in log.read_text("utf-8")
