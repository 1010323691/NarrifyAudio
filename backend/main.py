"""Narrify Audio backend — FastAPI application entrypoint.

Run (API only):      ``python -m backend.main``  →  http://127.0.0.1:8642
Run (whole app):     ``npm run build`` then the same command, and open
                     http://127.0.0.1:8642 — the backend also serves the built
                     frontend, so the full console runs on the backend alone.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .api import audio as api_audio
from .api import admin as api_admin
from .api import admin_resources as api_admin_resources
from .api import auth as api_auth
from .api import bgm as api_bgm
from .api import config as api_config
from .api import files as api_files
from .api import music as api_music
from .api import platform_tasks as api_platform_tasks
from .api import project_resources as api_project_resources
from .api import projects as api_projects
from .api import quota as api_quota
from .api import script as api_script
from .api import tasks as api_tasks
from .api import tts as api_tts
from .core import config as core_config
from .core import logging_setup
from .core.observability import record_api_request
from .core.request_context import bind_workspace, reset_workspace
from .core.paths import get_or_prepare_layout
from .services.bootstrap import ensure_bootstrap_admin
from .platform.config import settings
from .platform.database import initialize_schema
from .platform.database import SessionLocal
from .platform.deps import require_legacy_access
from .platform.project_context import active_project
from .platform.security import load_session
from .platform.storage import lock_storage_migration, storage_migration, project_workspace_path
from time import monotonic

PORT = 8642

# All module routers (tts drives the isolated local engine; script drives the
# LLM → JSON pipeline) plus the cross-cutting task / config / files routers.
PLATFORM_ROUTERS = [
    api_auth.router,
    api_projects.router,
    api_project_resources.router,
    api_quota.router,
    api_platform_tasks.router,
    api_admin.router,
    api_admin_resources.router,
]

LEGACY_ROUTERS = [
    api_tasks.router,
    api_config.router,
    api_files.router,
    api_audio.router,
    api_bgm.router,
    api_tts.router,
    api_script.router,
    api_music.router,
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_schema()
    ensure_bootstrap_admin()
    layout = get_or_prepare_layout()
    logging_setup.setup_logging(layout.logs, core_config.get_config().log.level)
    yield


app = FastAPI(title="NarrifyAudio API", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def collect_api_metrics(request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    started = monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_name = getattr(route, "path", "unmatched")
        record_api_request(route_name, status_code, (monotonic() - started) * 1000, request.method)


@app.middleware("http")
async def bind_authenticated_workspace(request, call_next):
    """Bind the managed workspace before sync dependencies/endpoints run.

    Starlette may execute a sync dependency and its endpoint in different
    worker threads.  Binding only inside an auth dependency therefore does not
    reliably reach the legacy engine call.  Middleware establishes the request
    context in the parent task, while the dependency still repeats ownership
    validation as defense in depth.
    """
    token = None
    session_token = request.cookies.get(settings.session_cookie)
    if session_token:
        with SessionLocal() as db:
            session = load_session(db, session_token)
            if session is not None:
                project = active_project(db, session.user, session)
                if project is not None:
                    token = bind_workspace(project_workspace_path(db, session.user.username, project.id))
                else:
                    token = bind_workspace(None)
    try:
        return await call_next(request)
    finally:
        if token is not None:
            reset_workspace(token)

@app.middleware("http")
async def protect_storage_during_migration(request, call_next):
    # Register outside workspace binding: resolving a root before acquiring
    # the lock could bind a request to the old root after a migration finishes.
    # Legacy GET handlers may prepare directories, so protect reads as well.
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    if request.url.path in {"/api/health", "/api/v1/admin/settings/storage", "/api/auth/login", "/api/auth/logout"}:
        return await call_next(request)

    def enter_storage():
        db = SessionLocal()
        try:
            if lock_storage_migration(db, shared=True) and storage_migration(db) is None:
                return db
        except BaseException:
            db.close()
            raise
        db.close()
        return None

    db = await run_in_threadpool(enter_storage)
    if db is None:
        return JSONResponse(status_code=409, content={"detail": "存储根目录正在迁移，工作空间暂时不可用"})
    try:
        return await call_next(request)
    finally:
        await run_in_threadpool(db.close)


# Local client on this machine; origins are loopback addresses.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in PLATFORM_ROUTERS:
    app.include_router(r)
for r in LEGACY_ROUTERS:
    # The old pipeline routes remain available during migration, but they no
    # longer form an unauthenticated local filesystem API.  The dependency also
    # requires CSRF for every state-changing legacy call.
    app.include_router(r, dependencies=[Depends(require_legacy_access)])


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "audiobookstudio-backend", "port": PORT}


# ---------------------------------------------------------------------------
# Static frontend (optional fallback).
#
# Serves the Vite build (``dist/``) so the entire console can run from the
# backend alone, without a Vite dev server:
#
#     npm run build            #  →  dist/
#     python -m backend.main   #  →  open http://127.0.0.1:8642
#
# Registered LAST, so every ``/api/...`` route (added above) still wins. Real
# assets resolve from ``dist/``; any other path falls back to ``index.html``
# so the client-side router can handle deep links (``/text``, ``/book`` …).
# When ``dist/`` is absent (e.g. during ``npm run dev``, where Vite serves the UI)
# these routes simply return 503 and the API is unaffected.
# ---------------------------------------------------------------------------
DIST_DIR = Path(__file__).resolve().parent.parent / "dist"


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str) -> Response:
    # Keep API 404s honest: an unknown /api/... is a real error, not the SPA shell.
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    if not DIST_DIR.is_dir():
        return Response(
            status_code=503,
            media_type="text/plain; charset=utf-8",
            content="Frontend build not found. Run `npm run build` first, then reload.",
        )

    candidate = (DIST_DIR / full_path).resolve() if full_path else DIST_DIR
    # Serve a real file (e.g. /assets/index-*.js) when it lives inside dist/.
    if full_path and candidate.is_file() and candidate.is_relative_to(DIST_DIR.resolve()):
        return FileResponse(candidate)

    # Otherwise return the SPA entry and let the client router take over.
    index = DIST_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return Response(
        status_code=503,
        media_type="text/plain; charset=utf-8",
        content="index.html missing in dist/. Run `npm run build`.",
    )


# The GET catch-all above only covers GET, so a non-GET request to an unknown
# /api/... path would otherwise fall through to FastAPI's 405 ("method not
# allowed" — wrong: the *route* doesn't exist). Answer 404 for those too, so
# every unknown /api/... is an honest "Not Found" regardless of verb. Real API
# routes are registered earlier and always win.
@app.api_route(
    "/api/{full_path:path}",
    methods=["POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
def api_not_found(full_path: str) -> Response:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=PORT, log_level="warning")
