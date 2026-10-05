"""Exercise SIGTERM against a real API server with an open SSE connection."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX SIGTERM handling")
@pytest.mark.parametrize("open_stream", [False, True])
def test_api_sigterm_exits_cleanly_with_or_without_sse(tmp_path, open_stream):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    # Keep the real app middleware, event generator and production server
    # options; replace DB polling and startup to isolate this shutdown test.
    script = """
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi.responses import StreamingResponse
from backend import main
from backend.services import task_views

root = Path(sys.argv[2])
main.HOST = "127.0.0.1"
main.PORT = int(sys.argv[1])
task_views.snapshot_payload = lambda rows: ([], {}, set(), {})
task_views.session_still_valid = lambda token, user: True
task_views._new_frames = lambda *args: []

@asynccontextmanager
async def lifespan(app):
    yield
    (root / "lifespan-closed").touch()

main.app.router.lifespan_context = lifespan

@main.app.get("/shutdown-stream")
async def stream():
    async def connected():
        return False
    async def body():
        generator = task_views.aggregate_stream(lambda db: [], "token", "user", connected)
        try:
            async for frame in generator:
                yield frame
        finally:
            await generator.aclose()
            (root / "stream-closed").touch()
    return StreamingResponse(body(), media_type="text/event-stream")

# Register before the frontend catch-all.
main.app.router.routes.insert(0, main.app.router.routes.pop())
main.run_api()
"""
    response = None
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(port), str(tmp_path)],
            cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                assert process.poll() is None, "API exited during startup"
                try:
                    with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as health:
                        assert health.status == 200
                    break
                except (URLError, TimeoutError):
                    assert time.monotonic() < deadline, "API did not start"
                    time.sleep(0.1)

            if open_stream:
                response = urlopen(f"http://127.0.0.1:{port}/shutdown-stream", timeout=10)
                assert response.readline().startswith(b"data:")

            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=12)
            assert time.monotonic() - started < 12
            # Recent Uvicorn re-raises the original signal after cleanup.
            assert process.returncode in (0, -signal.SIGTERM)
            assert (tmp_path / "lifespan-closed").exists()
            if open_stream:
                assert (tmp_path / "stream-closed").exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if response is not None:
                response.close()
