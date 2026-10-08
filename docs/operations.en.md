# Operations, configuration, and data

[Home](../README.md) · [中文](operations.md) · [Windows deployment](windows.en.md) · [Linux deployment](linux.en.md) · [Production guide](production.en.md) · [Development](development.en.md)

## Three configuration layers

| Configuration | Location | Management |
| --- | --- | --- |
| Database, Redis, cookies, connection pools, bootstrap admin | Process `NARRIFY_*` environment, usually saved locally in `.env` | Load as described in the deployment guide; restart affected processes after changes |
| Registration, initial character quota, LLM, production defaults, GPU scheduler, storage root | Platform settings in PostgreSQL | Admin console |
| Project production settings and working data | Project workspace, including `config/setting.json` | Project workbench and application configuration |

API, Worker, and Alembic do not load `.env` themselves. Windows `launch/start.ps1` imports its `NARRIFY_*` values; Linux uses the environment file or launch scripts described in its deployment guide. Load the same environment before running modules separately.

`.env.example` lists deployment variables. `NARRIFY_STORAGE_ROOT` defaults to the repository's `storage/`; a storage root saved in the admin console takes precedence. Use the console's migration operation rather than moving folders manually or editing paths. Migration requires drained tasks and validates destination conflicts.

The tracked root `setting.json` is a default template: keep its workspace path empty and use only the `local` placeholder for the LLM key when committing. Project configuration and legacy `config/app.json` are user data. Reads prefer `setting.json`; saving creates the new configuration while retaining the old file. Keep credentials, local paths, logs, and workspaces out of version control.

Registration is enabled by default and new users start with 0 character quota. Administrators can close registration, set initial quota, and adjust account balances. LLM billing counts accepted business-output characters; TTS reserves input-character quota and settles it when results are published. This application quota is distinct from upstream token usage or monetary charges. Bootstrap administrator settings initialize the account only; changing the bootstrap password in `.env` does not reset an existing account.

Browsers use Cookie sessions and CSRF. For external deployments, follow platform guidance for HTTPS, `NARRIFY_COOKIE_SECURE`, and allowed origins; prefer a same-origin reverse proxy. Separate frontend domains use build-time `VITE_API_BASE` and `VITE_CSRF_COOKIE_NAME`; rebuild after changing them and review backend origin/Cookie policies.

## Worker pool and concurrency

The standard entrypoint is `python -m backend.worker_pool`. It supervises **4 mechanical Workers and 4 model Workers** by default, restarting exited slots with backoff. Mechanical tasks include formatting, splitting, resource scanning, merging, and mixing. Model tasks include parsing, character analysis/cloning, synthesis, preview rendering, BGM scene analysis, and music tag suggestions.

Change process counts using `--mechanical-workers` and `--model-workers`; both must be at least 1. `backend.worker` alone defaults to mechanical tasks; model debugging requires `--task-lane model`. A single default Worker does not provide the complete task service.

Platform `generation.parse_worker_concurrency` sets the shared LLM request limit, default 4, across all model Workers. More processes do not multiply it. A separate host-wide limit caps active LLM tasks at twice that setting. Tasks, processes, threads, and concurrent model requests are different quantities. TTS permits and GPU scheduling have separate controls. Conflicting project writes queue; non-conflicting merges/mixes for different chapters can run concurrently.

Business and request-lock database pools are separate. Default limits are `(16+0)+(8+0)=24` for one API and `(6+0)+(1+0)=7` per Worker: **80 connections** for one API and eight Workers. This is a pool ceiling, not a fixed startup allocation. Reserve headroom for migrations, administration, and other applications. Recalculate before adding APIs or Workers; see `.env.example` for variables. Worker business connections wait up to 30 seconds by default; API and lock connections wait up to 3 seconds. Explicit environment settings take precedence: review them and restart after upgrading. Workers retry transient database failures with backoff; investigate the underlying failure.

## Optional local single-GPU service scheduling

Scheduling is disabled by default. It switches local LLM and TTS services when they share a GPU. Remote LLM services can support production but are outside this scheduler's management.

Before enabling it:

1. Verify the NVIDIA driver, CUDA PyTorch, TTS dependencies, and models with real short-text synthesis. Verify an API call to the configured LLM model.
2. Configure the platform LLM URL, model, and credentials in the admin console. Scheduling requires HTTP/HTTPS with host `localhost`, `127.0.0.1`, or `::1`, and a non-empty model name.
3. Configure an absolute path to the foreground LLM start script; a stop script is optional. Existing `.ps1`, `.cmd`, `.bat`, or `.sh` files must match the host platform. Keep the LLM process lifecycle manageable instead of detaching it as an unsupervised background service.
4. Stop the independently running LLM, check other services using the same GPU, enable scheduling in the admin Worker / Queue settings, and keep model Workers online.

Settings cover minimum runtime, switch cooldown, queue difference, maximum wait, startup/stop/drain timeouts, and idle shutdown. Start with defaults and tune using actual memory and wait behavior. For ERROR states, inspect events and service logs, repair scripts or models, then use recovery. Recovery does not install drivers or models.

## Backups, upgrades, and legacy layouts

**Back up PostgreSQL, the actual storage root, and local configuration together.** A deliverable ZIP is not a complete project backup and cannot restore database records. Also preserve the actual music library when using your own BGM. Keeping model caches avoids repeated downloads.

After stopping application writes, run backups from the repository root. These examples use default database/user names; adjust them and replace the storage placeholder with the actual root shown in the admin console. Keep backups outside the storage root, check command results and contents, and copy completed backups to separate backup media. Restrict access: they contain credentials and user data.

```powershell
# Windows; enter the original application database password at the prompt
$backupDir = Join-Path '.backups' ('narrify-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$storageRoot = 'D:\replace-with-actual-storage-root'
& "$env:ProgramFiles\PostgreSQL\16\bin\pg_dump.exe" -h 127.0.0.1 -p 5432 -U narrify -d narrify -W -Fc -f (Join-Path $backupDir 'database.dump')
if ($LASTEXITCODE -ne 0) { throw 'Database backup failed' }
Copy-Item -LiteralPath $storageRoot -Destination (Join-Path $backupDir 'storage') -Recurse -ErrorAction Stop
foreach ($item in @('.env', 'setting.json', 'app.json', 'config', 'logs', '.narrify', 'music_library')) {
  if (Test-Path -LiteralPath $item) { Copy-Item -LiteralPath $item -Destination $backupDir -Recurse -ErrorAction Stop }
}
```

```bash
# Linux; this account must be able to read application files and access PostgreSQL
umask 077
backup_dir="$HOME/narrify-backups/$(date +%Y%m%d-%H%M%S)"
storage_root='/replace-with-actual-storage-root'
mkdir -p "$backup_dir"
pg_dump -h 127.0.0.1 -p 5432 -U narrify -d narrify -W -Fc -f "$backup_dir/database.dump" || exit 1
tar -czf "$backup_dir/storage.tar.gz" -C "$storage_root" . || exit 1
runtime_paths=()
for item in .env setting.json app.json config logs .narrify music_library; do
  if [ -e "$item" ]; then runtime_paths+=("$item"); fi
done
if [ "${#runtime_paths[@]}" -gt 0 ]; then
  tar -czf "$backup_dir/runtime.tar.gz" "${runtime_paths[@]}" || exit 1
fi
```

If Linux systemd environment files live outside the repository (for example, under `/etc/narrify-audio/`), back them up separately with the appropriate permissions; a repository `.env` is insufficient. Preserve external music libraries and model caches at their actual locations as needed.

Before upgrading, finish or cancel tasks and verify that pending, paused, or retrying tasks have been handled. Stop API, Worker pool, and frontend. Keep data services running for backup and migration. Back up PostgreSQL (for example, `pg_dump -Fc`), all actual workspaces, `.env`, and local configuration. Then update source, run `npm.cmd ci` on Windows or `npm ci` on Linux, and review basic/TTS dependency changes.

- **Windows:** Start as described in the [Windows guide](windows.en.md). `launch/start.ps1` runs Alembic before the API starts. Log in and execute a basic text task after upgrading.
- **Linux:** Update the build and systemd configuration following the [Linux guide](linux.en.md). A migration unit using `RemainAfterExit` must be explicitly restarted to run again. After changing units or the Worker entrypoint, run daemon-reload and restart services. Updating source does not rewrite installed units.

To migrate legacy UUID workspace layouts, perform the same backups and shutdown, and handle unfinished tasks first. With the application's `NARRIFY_*` environment loaded, run:

```powershell
# Windows, repository root
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m backend.services.workspace_migration
```

```bash
# Linux, repository root
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m backend.services.workspace_migration
```

Layout migration normalizes project-name directories and legacy input layouts and updates path references while preserving database IDs. It is repeatable and refuses to run with unfinished tasks or an in-use workspace. Check original text, voices, audio, and configuration afterward, then validate parsing, synthesis, and delivery. Do not upgrade by deleting and reinitializing the database.

The storage root's `.layout-migrations/` contains move/rollback journals. Do not delete them while migration or recovery is unfinished. Migration retains unregistered historical files: unclassified legacy files/directories move to the project's `07_output/历史文件/`, while old caches move into temporary storage. Preservation does not confer deliverable download eligibility.

## Project workspace

Managed projects use `storage root/username/project name/`. Database IDs remain stable; code resolves physical paths through `Project.directory_key` rather than constructing paths from project IDs.

```text
00_temp/           Temporary files, task attempts, publication journals
01_input/          Uploaded source text
02_split_text/     Formatted text and split chapters
03_parsed_json/    Book, script, and audio analysis
04_voice_profiles/ Character and voice data
05_audio_chunk/    Synthesized chunks
06_audio_merge/    Merged audio
07_output/         Deliverables and other persistent output
08_bgm/            BGM analysis and mixing data
config/            Project configuration
logs/              Project logs
```

Names follow Windows folder restrictions and are case-insensitive. Projects with unfinished tasks cannot be renamed. Application renaming updates directories, database records, and path references; do not rename project folders manually. Same-name uploads/results that preserve history use suffixes such as `file (2).ext`; tasks explicitly updating production modules retain their module update rules. Trashed directories use `项目名称（回收站 N）`. Restoration attempts the original name and uses the `（恢复）` suffix for conflicts.

## Resources and troubleshooting

My Resources uses Worker scans to build its inventory. Refresh after external workspace edits; this is not live filesystem monitoring. Failed scans retain the previous complete inventory. Resolve scan errors before treating partial inventories or unknown sizes as current totals.

Only validated deliverables can be downloaded or packaged. Synthesis chunks, working materials, and incomplete merged audio are unavailable for delivery. Export packages expire after 7 days and become unavailable when a source project enters trash. Cache cleanup targets eligible ordinary files older than 7 days in approved temporary/cache directories. Deletion is irreversible, and cancellation does not restore files already removed. Projects remain in trash for one calendar month before Worker cleanup.

If pages load but tasks do not run, check PostgreSQL, Redis, online Workers for both lanes, Outbox/queue, task events, and quota. `/api/health` does not check the database or Workers; validate by logging in and running a real task. For parsing failures, check LLM URL, model, credentials, and quota. For TTS failures, inspect subprocess logs, drivers/PyTorch, models, and FFmpeg. Investigate the first error before repeatedly resubmitting failing tasks.
