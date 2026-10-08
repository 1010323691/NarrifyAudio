# Development guide

[Home](../README.md) · [中文](development.md) · [Windows deployment](windows.en.md) · [Linux deployment](linux.en.md) · [Production guide](production.en.md) · [Operations](operations.en.md)

## Architecture and entrypoints

The frontend uses Vue 3, TypeScript, Pinia, and Vue Router. `src/api/` contains request clients, `src/stores/` holds state, `src/views/` contains pages, and `src/composables/` organizes workbench logic. Shared components and layouts live in `src/components/` and `src/layouts/`.

The backend follows **api → services → platform → core**, enforced by `.importlinter`. Routes call business services; the platform layer owns authentication, database, tasks, quota, and storage; core supplies infrastructure. `engines/` and API/Worker entrypoints are outside this four-layer contract; that is not permission to introduce reverse dependencies.

`backend.main` serves the API, `backend.worker_pool` supervises mechanical/model Workers, and `backend.worker` executes lane-specific work. PostgreSQL stores users, projects, tasks, events, and Outbox records; a Redis-protocol service provides the queue. Workers execute long production operations. TTS runs in independent subprocesses under `tts-engine/` with dependencies in the shared `.venv`. FastAPI must not import torch or model code.

## Tasks and workbenches

`backend/platform/task_registry.py` is the single source of truth for task types. When adding or changing a task, check registration, submission validation, dispatch, billing/permissions, GPU classification, and frontend types/labels together. Use `/api/v1/tasks*` for durable task lists, history, events, cancellation, and retries rather than creating a parallel task-management path.

Frontend task events use the shared SSE channel in `src/stores/task.ts`. HTTP requests go through `src/api/client.ts` using Cookie sessions and CSRF. Account/project switches must invalidate old requests so stale results cannot write back. Handle disconnections and snapshot resynchronization. Resource scans, packaging, and cleanup also run in Workers; do not recursively scan workspaces or execute long operations inside API requests.

Database model changes need Alembic migrations, keeping the migration head consistent with models. Path and subprocess changes require attention to Windows/Linux differences, file identity, symlinks/junctions, cancellation, and publication boundaries. User files and runtime configuration are not source-cleanup targets.

## Environment and commands

Set up `.venv` and frontend dependencies using the deployment guide. Basic runtime and development checks do not require a GPU. The complete `backend/requirements.txt` combines application, audio, TTS, and test dependencies; installing it directly pulls large model dependencies. Follow the deployment guide's basic package list; install TTS and validate real synthesis separately.

For an existing basic environment, add test tools and the audio libraries used by tests:

```powershell
.\.venv\Scripts\python.exe -m pip install 'pytest>=9.1' 'pytest-xdist>=3.8' 'httpx2>=2.13' import-linter 'soundfile>=0.12' 'pydub>=0.25' 'numpy>=2.0'
npm.cmd ci
npm.cmd run dev
.\.venv\Scripts\python.exe -m backend.main
.\.venv\Scripts\python.exe -m backend.worker_pool
```

Run `dev`, the API, and the Worker pool in three separate terminals: each is a long-running process. Load the application's `NARRIFY_*` environment in each backend terminal before starting its module. Linux uses `.venv/bin/python` and `npm`; see deployment guides for combined launch scripts. Avoid duplicate API/Worker instances without accounting for database and GPU capacity.

```powershell
npm.cmd run typecheck
npm.cmd run build
npm.cmd run build:all
npm.cmd run lint:imports
.\.venv\Scripts\python.exe -m pytest backend/tests -n 4 --dist loadscope
```

`build` performs Vue/TypeScript checks and a Vite production build. `build:all` adds backend/tts-engine compilation and the import-layer check; it does not replace tests. Do not edit or commit generated `dist/` files.

Production frontend requests default to same-origin `/api`, with FastAPI serving `dist/` or a reverse proxy providing one origin. For separate domains, set `VITE_API_BASE` before building. If the backend changes `NARRIFY_CSRF_COOKIE`, set the corresponding `VITE_CSRF_COOKIE_NAME`. These values are embedded in build output and require a rebuild after changes. Also configure backend `NARRIFY_CORS_ORIGINS` and Cookie/HTTPS policies as described in deployment and operations guides.

## Regression checks and CI

The local full backend suite must use **`-n 4 --dist loadscope`**. Module-level database state requires tests in the same file to stay on one worker; do not use `-n auto` or omit `loadscope`. Tests override deployment environment variables and use temporary SQLite/storage, so PostgreSQL/Memurai are not required by default. Real-database, Redis, FFmpeg, and platform-specific tests run or skip according to detected conditions. Read skip reasons; a skip is not real-service validation. Real GPU synthesis needs separate smoke testing.

Choose frontend regressions by the changed area:

| Area | npm scripts |
| --- | --- |
| Pinia isolation | `test:state-isolation` |
| Chapter review / script parsing / voices | `test:workbench` / `test:script-parse-workbench` / `test:voices-workbench` |
| Production / overview / project cards | `test:production-workbench` / `test:project-overview` / `test:project-cards` |
| Task center / resources / admin / GPU settings | `test:task-center` / `test:resources` / `test:admin-console` / `test:gpu-scheduler` |
| Layout | `test:mobile-layout` / `test:ten-row-layout`, plus relevant `*-browser` scripts |

`package.json` is the complete script reference. Read browser-test scripts for startup requirements; browser tests differ from sandbox regressions. Run typecheck/build for frontend or shared-flow changes, lint:imports for backend dependency changes, and the full backend suite for backend modifications as required by repository guidance.

Current GitHub CI uses Node 22 for production builds and selected state, overview, task, production, resource, admin, and workbench regressions. Its Python 3.14 backend job installs FFmpeg, excludes model dependencies, and runs compilation, import checks, and the full suite. The two-core CI runner uses **`-n 2 --dist loadscope -rs`**, separate from the local four-worker rule. CI does not validate real GPU models.

## Contributions and references

Use 4-space Python indentation and the existing 2-space TypeScript/Vue style. Reuse task, file, and utility code. Review the full diff before committing and include only relevant source/documents. Use Conventional Commits; exclude secrets, virtual environments, build output, logs, generated audio, and workspaces. Follow the user's current authorization for commits and pushes; completing a fix does not authorize a commit.

Check model, dependency, and asset licenses separately. The project license is [LICENSE](../LICENSE). See [Operations](operations.en.md) for production configuration, connection budgets, GPU scheduling, and data migration.
