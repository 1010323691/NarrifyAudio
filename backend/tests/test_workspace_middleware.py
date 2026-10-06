import asyncio
import threading
from types import SimpleNamespace

from backend import main
from backend.core.request_context import bound_workspace
from backend.platform.platform_settings import settings


def test_workspace_pool_wait_does_not_block_event_loop_or_lose_request_context(monkeypatch, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    session = SimpleNamespace(user=SimpleNamespace(username="user"))
    request = SimpleNamespace(cookies={settings.session_cookie: "test-session"})

    class WaitingSession:
        def __enter__(self):
            entered.set()
            assert release.wait(2), "connection checkout blocked the event loop"
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "SessionLocal", WaitingSession)
    monkeypatch.setattr(main, "load_session", lambda *args: session)
    monkeypatch.setattr(main, "active_project", lambda *args: SimpleNamespace(id="project"))
    monkeypatch.setattr(main, "project_workspace_path", lambda *args: tmp_path)

    async def run():
        previous = bound_workspace()

        async def endpoint(request):
            assert bound_workspace() == tmp_path.resolve()
            await asyncio.sleep(0)
            assert bound_workspace() == tmp_path.resolve()
            return "ok"

        async def handle():
            result = await main.bind_authenticated_workspace(request, endpoint)
            assert bound_workspace() is previous
            return result

        task = asyncio.create_task(handle())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            release.set()
            assert await task == "ok"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
