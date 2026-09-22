"""AudiobookStudio backend — FastAPI application entrypoint.

Run (API only):      ``python -m backend.main``  →  http://127.0.0.1:8642
Run (whole app):     ``npm run build`` then the same command, and open
                     http://127.0.0.1:8642 — the backend also serves the built
                     frontend, so the full console runs on the backend alone.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from .api import audio as api_audio
from .api import admin as api_admin
from .api import auth as api_auth
from .api import bgm as api_bgm
from .api import book as api_book
from .api import config as api_config
from .api import files as api_files
from .api import filesystem as api_filesystem
from .api import music as api_music
from .api import platform_tasks as api_platform_tasks
from .api import projects as api_projects
from .api import quota as api_quota
from .api import workspaces as api_workspaces
from .api import script as api_script
from .api import tasks as api_tasks
from .api import text as api_text
from .api import tts as api_tts
from .api import workspace as api_workspace
from .core import config as core_config
from .core import logging_setup
from .core.paths import get_layout
from .platform.bootstrap import ensure_bootstrap_admin
from .platform.database import initialize_schema

PORT = 8642

# All module routers (tts drives the isolated local engine; script drives the
# LLM → JSON pipeline) plus the cross-cutting task / config / files routers.
ROUTERS = [
    api_auth.router,
    api_projects.router,
    api_quota.router,
    api_workspaces.router,
    api_platform_tasks.router,
    api_admin.router,
    api_tasks.router,
    api_config.router,
    api_files.router,
    api_filesystem.router,
    api_text.router,
    api_book.router,
    api_audio.router,
    api_bgm.router,
    api_tts.router,
    api_script.router,
    api_workspace.router,
    api_music.router,
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_schema()
    ensure_bootstrap_admin()
    layout = get_layout()
    logging_setup.setup_logging(layout.logs, core_config.get_config().log.level)
    yield


app = FastAPI(title="NarrifyAudio API", version="0.2.0", lifespan=lifespan)

# Local client on this machine; origins are loopback addresses.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in ROUTERS:
    app.include_router(r)


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
