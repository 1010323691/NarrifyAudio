# Linux deployment

[中文](../readme-linux.md) · [Home](../README.md) · [Windows](windows.en.md) · [Operations](operations.en.md) · [Development](development.en.md) · [Production workflow](production.en.md)

This guide targets a fresh **Ubuntu Server 24.04 LTS x86_64** system using system packages, a project Python environment and systemd. Other distributions need adjusted package names, PostgreSQL cluster names and service configuration.

Install system tools → PostgreSQL / Redis → project dependencies and frontend build → database and environment → migrations → API / Worker pool → Nginx → LLM / TTS → production acceptance.

The guide is based on repository implementation and upstream installation documentation. The development machine is Windows; Linux hardware deployment, CUDA inference and cancellation still require acceptance on the target host. Target versions below are not claims about the latest releases or universal compatibility.

## Differences from Windows

| Component | Windows local deployment | This Linux deployment |
| --- | --- | --- |
| Python | Installed Python 3.14 | Ubuntu Python 3.12 in `.venv` |
| Node.js | Node.js 24 LTS MSI / winget | NodeSource Node.js 24 DEB |
| Database | PostgreSQL 16 Windows service | APT PostgreSQL 16 / systemd |
| Redis protocol | Memurai Windows service | APT Redis / `redis-server` |
| API / Worker pool | Local processes | Separate systemd services |
| Frontend | Vite :5173 | Built `dist/` via API and Nginx |
| Environment | Launcher loads `.env` | Shared systemd `EnvironmentFile` |
| TTS | `install_tts_env.ps1` | Python dependencies and CUDA wheels in `.venv` |
| Audio tools | winget FFmpeg / SoX_ng | APT FFmpeg / SoX; no `.exe` alias |
| Logs | Consoles and application logs | journald and application/task logs |

Windows `.ps1` / `.bat` files do not apply to Linux. Bash launchers manage an installed stack; they do not install system dependencies, initialize the database or create systemd units.

## Linux launchers

After configuring PostgreSQL, Redis, the frontend build and the three application units below, run from the repository root:

```bash
bash launch/start-data-services.sh # Start PostgreSQL and Redis
bash launch/start.sh               # Start data services; migrate when the application is stopped; start API and Worker pool
bash launch/stop.sh                # Stop API and Worker pool before data services (whole-stack shutdown)
bash launch/stop-data-services.sh  # Stop only data services (application must already be stopped)
```

System deployments use sudo when required; API and Workers still run as `narrify` with `/etc/narrify-audio/narrify.env`. Default data services are `postgresql@16-main.service` and `redis-server.service`. Set `POSTGRES_SERVICE` / `REDIS_SERVICE` to override names and adjust unit dependencies too.

The scripts switch to `systemctl --user` only if all five user unit files exist under `~/.config/systemd/user/`; default data names become `narrify-postgresql.service` and `narrify-redis.service`. Use `journalctl --user -u narrify-api -u narrify-worker` for this mode. Production startup uses built `dist/` or Nginx and does not run Vite.

For development, a separate foreground launcher is available:

```bash
# Install Linux .venv and npm dependencies; configure repository .env; start data services first.
# Stop existing API, Worker and Vite instances to avoid duplicate processes.
bash launch/dev-all.sh
```

It requires Bash 4.3+, curl, flock and setsid (util-linux), plus Linux `.venv/bin/python`. It parses only `NARRIFY_*` from `.env` as data, without evaluating shell code; migrates; starts API, Worker pool and Vite on `127.0.0.1:5173`; and writes background logs to `logs/dev/`. A lock rejects duplicate startup. If any component exits, the launcher ends the development stack. Ctrl+C stops its own process groups, forcefully after at most ten seconds; data services remain running. Data-service shutdown is refused while this development launcher owns the lock.

Scripts use LF. Invoke with `bash launch/name.sh`, or execute directly; entrypoints invoked with `sh` switch to Bash. From `launch/`, `bash start.sh` also works.

## User-level deployment

This is an advanced deployment description, not a complete source-build and user-unit installation guide. The numbered installation below uses system services. Without sudo or system packages, you may separately compile PostgreSQL 16 and Redis under `~/.local`, then install five units: `narrify-postgresql`, `narrify-redis`, `narrify-migrate`, `narrify-api`, `narrify-worker`.

All five unit files must exist for automatic user-mode detection; a partial set falls back to the system deployment. Units still provide settings through `EnvironmentFile` (for example repository `.env`); production scripts never source it. User services normally end when all login sessions close. Keeping them alive after logout requires `sudo loginctl enable-linger <user>`, which itself requires administrator rights.

## Running architecture

```mermaid
flowchart LR
  Browser[Browser] <-->|HTTP / HTTPS| Nginx[Nginx :80 / :443]
  Nginx <-->|Pages, API and SSE| API[FastAPI 127.0.0.1:8642]
  Dist[Frontend build dist/] --> API
  API -->|Business records and task Outbox| PG[PostgreSQL 16 :5432]
  API <--> Redis[Redis :6379]
  Worker[Worker pool: 4 mechanical + 4 model] <-->|Claim tasks and save results| PG
  Worker <--> Redis
  Worker --> Storage[Persistent workspace]
  Worker --> LLM[Accessible LLM API]
  Worker --> TTS[Isolated TTS subprocess]
  TTS --> GPU[NVIDIA GPU / PyTorch / models]
  Worker --> Audio[FFmpeg / ffprobe / SoX]
```

After building, Vite and Node.js need not remain running. Persistent services are PostgreSQL, Redis, API, Worker pool and Nginx, with a separately managed local LLM if used. Workers spawn TTS per task; no Qwen demo or separate TTS HTTP service is required.

Basic login, project management and formatting require no CUDA. Script/role analysis needs LLM access. Local speech and complete audio production additionally need TTS packages, models and audio tools. CPU device selection exists in code, but speed, memory and actual compatibility need separate testing.

## 1. Install system components

Use Bash on the Linux host with a sudo-enabled login account, checking each step before continuing. Do not run these commands in Windows PowerShell.

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential pkg-config \
  python3 python3-venv python3-pip \
  postgresql-16 postgresql-client-16 redis-server nginx \
  ffmpeg sox libsox-fmt-all libsndfile1
```

Ubuntu 24.04 supplies PostgreSQL 16; specifying the major version avoids silently using a different database version on another OS. See [Ubuntu 24.04 release notes](https://documentation.ubuntu.com/release-notes/24.04/).

Install Node.js 24 using the NodeSource APT repository. The downloaded script configures APT; this recipe does not use nvm or portable Node. See [NodeSource instructions](https://github.com/nodesource/distributions/blob/master/DEV_README.md).

```bash
curl -fsSL https://deb.nodesource.com/setup_24.x -o /tmp/narrify-nodesource-setup.sh
sudo bash /tmp/narrify-nodesource-setup.sh
sudo apt install -y nodejs

python3 --version
node --version
npm --version
psql --version
redis-server --version
ffmpeg -version
ffprobe -version
sox --version
```

Expected major versions are Python 3.12, Node.js 24 and PostgreSQL 16. Python 3.12 follows the [Qwen3-TTS environment recommendation](https://github.com/QwenLM/Qwen3-TTS#environment-setup); Windows' Python version and Conda are not required for this Linux recipe.

## 2. Configure data services

```bash
sudo systemctl enable --now postgresql redis-server
pg_lsclusters
pg_isready -h 127.0.0.1 -p 5432
redis-cli -h 127.0.0.1 ping
```

Expect cluster `16/main` online and Redis `PONG`. `postgresql.service` is the cluster umbrella; the actual default PostgreSQL instance is `postgresql@16-main.service`.

Bind PostgreSQL and Redis to loopback. Preserve Redis `protected-mode yes`; neither service needs browser access or public ports 5432/6379. Task streams need persistence: edit the existing setting in `/etc/redis/redis.conf` to `appendonly yes`, retaining `appendfsync everysec`, then restart and verify:

```bash
sudo systemctl restart redis-server
redis-cli CONFIG GET appendonly
redis-cli CONFIG GET appendfsync
```

Create a dedicated application role. Set its password interactively rather than in a shell command:

```bash
sudo -u postgres createuser --no-superuser --no-createdb --no-createrole narrify
sudo -u postgres psql
```

At the psql prompt:

```text
\password narrify
\q
```

Create the UTF8 database and verify TCP authentication:

```bash
sudo -u postgres createdb --owner=narrify --encoding=UTF8 --template=template0 narrify
psql -h 127.0.0.1 -U narrify -d narrify -W -c 'SHOW server_encoding;'
```

Use a long random password. URL-encode special characters such as `@`, `:`, `/`, `#` and `%` in the connection URL; psql takes the original password. If TCP authentication fails, check loopback host rules in `/etc/postgresql/16/main/pg_hba.conf` for `scram-sha-256`; do not replace authentication with `trust`. See [Ubuntu PostgreSQL setup](https://documentation.ubuntu.com/server/how-to/databases/install-postgresql/index.html).

These commands are for first installation. Check existing roles/databases rather than recreating or deleting them.

## 3. Create the runtime account, checkout and build

| Path | Purpose |
| --- | --- |
| `/opt/narrify-audio` | Source, `.venv`, `dist/` and repository runtime files |
| `/etc/narrify-audio/narrify.env` | Deployment secrets and settings |
| `/var/lib/narrify-audio/storage` | User/project workspaces |
| `/var/lib/narrify-audio/huggingface` | Shared model cache for the service account |
| `/var/backups/narrify-audio` | Database and file backups |

```bash
sudo useradd --system --user-group --create-home \
  --home-dir /var/lib/narrify-audio --shell /usr/sbin/nologin narrify
sudo install -d -o narrify -g narrify /opt/narrify-audio \
  /var/lib/narrify-audio/storage /var/lib/narrify-audio/huggingface
sudo install -d -o root -g narrify -m 0750 /etc/narrify-audio
sudo -u narrify -H git clone https://github.com/1010323691/NarrifyAudio.git /opt/narrify-audio
sudo -u narrify -H python3 -m venv /opt/narrify-audio/.venv
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install --upgrade pip
```

Install basic dependencies now; add TTS after validating the database/task chain:

```bash
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install \
  'fastapi>=0.115' 'uvicorn>=0.30' 'pydantic>=2.8' 'python-multipart>=0.0.9' \
  'SQLAlchemy>=2.0.36' 'alembic>=1.14' 'psycopg[binary]>=3.2' \
  'redis>=5.2' 'psutil>=6.0'
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip check
```

This subset reflects current application import requirements. `backend/requirements.txt` also contains TTS and tests; the complete installation is in section 8. Recheck the subset when dependency declarations change.

Build the frontend:

```bash
sudo -u narrify -H bash -c 'cd /opt/narrify-audio && npm ci && npm run build'
```

Use same-origin `/api`; do not set `VITE_API_BASE`. In particular, a built URL of `127.0.0.1:8642` would point a remote browser at its own machine. Check existing `.env*` files for leftover cross-origin build settings.

API, Workers and isolated TTS share `.venv`. The application may write repository `setting.json`, `config/`, `logs/`, `.narrify/` and `music_library/`. The runtime account therefore owns the project directory; do not mount the whole checkout read-only.

## 4. Save deployment environment

```bash
sudo install -o root -g narrify -m 0640 /dev/null /etc/narrify-audio/narrify.env
sudoedit /etc/narrify-audio/narrify.env
```

Replace every password placeholder:

```dotenv
NARRIFY_DATABASE_URL=postgresql+psycopg://narrify:REPLACE_DATABASE_PASSWORD@127.0.0.1:5432/narrify
NARRIFY_REDIS_URL=redis://127.0.0.1:6379/0
NARRIFY_STORAGE_ROOT=/var/lib/narrify-audio/storage
NARRIFY_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
NARRIFY_BOOTSTRAP_ADMIN_PASSWORD=REPLACE_ADMIN_PASSWORD
NARRIFY_REGISTRATION_ENABLED=false
NARRIFY_AUTO_CREATE_SCHEMA=false
NARRIFY_COOKIE_SECURE=false
HOME=/var/lib/narrify-audio
HF_HOME=/var/lib/narrify-audio/huggingface
PYTHONUTF8=1
PYTHONIOENCODING=utf-8
PYTHONUNBUFFERED=1
PATH=/opt/narrify-audio/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

This is a systemd environment file: one `KEY=value` per line, without export, command substitutions or references to other variables. Keep it private and out of Git. API/Workers do not load repository `.env` themselves; manually invoked Python commands also do not acquire this environment automatically.

Public registration is disabled in this example and can be changed after basic acceptance. Initial API startup creates the bootstrap administrator; an existing account's password is not reset. Configure LLM secrets, models and production parameters in the admin console.

`NARRIFY_STORAGE_ROOT` is a deployment default. Database `storage.root` saved by the administrator takes priority; inspect this especially when restoring an older database.

## 5. Run migrations and processes with systemd

### Database connection budgets and recovery

Defaults per process are API business and lock pools `16+8` each; Worker business pool `3+1`, lock pool `1+0`. Pools connect only when used. One API plus eight Workers (4 mechanical + 4 model) has a maximum budget of `48 + 8×5 = 88`, leaving twelve connections for migrations, maintenance and other clients when PostgreSQL has `max_connections=100`. Recalculate the total before increasing API/Worker processes or parse concurrency.

`.env.example` lists `NARRIFY_API_DB_*` / `NARRIFY_WORKER_DB_*`. Pool size must be positive; overflow may be zero but not unlimited. Default pool timeouts are API business 60 seconds, API lock 30, both Worker pools 10. Restart affected processes after edits. Standard `backend.worker` subprocesses select Worker defaults; embedded launchers may set `NARRIFY_DB_ROLE=worker`. Do not force that role in a shared API/Worker environment.

Long-running Workers retry transient database disconnects, connection exhaustion and pool timeouts during startup/heartbeat/claim/scheduling, backing off 1, 2, 4 seconds up to thirty. Lease/fencing protects existing tasks; execution is not blindly replayed. Failed error reporting does not overwrite the original exception. `--once` fails immediately; invalid credentials and SQL programming faults do not retry forever.

Create the following three files with sudoedit. Share a working directory, interpreter and environment; migrations belong to the separate oneshot unit.

### `/etc/systemd/system/narrify-migrate.service`

```ini
[Unit]
Description=Narrify Audio database migration
Requires=postgresql@16-main.service
After=postgresql@16-main.service

[Service]
Type=oneshot
User=narrify
Group=narrify
WorkingDirectory=/opt/narrify-audio
EnvironmentFile=/etc/narrify-audio/narrify.env
ExecStart=/opt/narrify-audio/.venv/bin/python -m alembic upgrade head
RemainAfterExit=yes
UMask=0027
```

### `/etc/systemd/system/narrify-api.service`

```ini
[Unit]
Description=Narrify Audio API
Wants=network-online.target
Requires=narrify-migrate.service redis-server.service
After=network-online.target narrify-migrate.service redis-server.service

[Service]
Type=simple
User=narrify
Group=narrify
WorkingDirectory=/opt/narrify-audio
EnvironmentFile=/etc/narrify-audio/narrify.env
ExecStart=/opt/narrify-audio/.venv/bin/python -m backend.main
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group
UMask=0027

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/narrify-worker.service`

```ini
[Unit]
Description=Narrify Audio task worker
Wants=network-online.target
Requires=narrify-migrate.service redis-server.service
After=network-online.target narrify-migrate.service redis-server.service

[Service]
Type=simple
User=narrify
Group=narrify
WorkingDirectory=/opt/narrify-audio
EnvironmentFile=/etc/narrify-audio/narrify.env
ExecStart=/opt/narrify-audio/.venv/bin/python -m backend.worker_pool
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
KillMode=control-group
UMask=0027

[Install]
WantedBy=multi-user.target
```

Adjust migration Requires/After if your PostgreSQL cluster differs. Do not substitute `NARRIFY_AUTO_CREATE_SCHEMA=true` for Alembic.

Load units and start:

```bash
sudo systemctl daemon-reload
sudo systemctl start narrify-migrate
sudo systemctl status narrify-migrate --no-pager
sudo systemctl enable --now narrify-api narrify-worker
sudo systemctl status narrify-api narrify-worker --no-pager
curl -fsS http://127.0.0.1:8642/api/health
```

After successful migration, `active (exited)` is normal. API defaults to `127.0.0.1:8642`; Nginx provides browser access. Deploy one API process and one Worker pool service, whose default is four mechanical plus four model processes. Before expanding, assess database budget, GPU memory and model permits; see [development](development.en.md).

## 6. Configure the Nginx same-origin entrance

Save `/etc/nginx/sites-available/narrify-audio` using sudoedit:

```nginx
server {
    listen 80 default_server;
    server_name _;

    # Backend default file limit: 50 MiB; allow extra room for multipart requests.
    client_max_body_size 64m;

    location / {
        proxy_pass http://127.0.0.1:8642;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";

        # Deliver SSE task events without proxy buffering.
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

API serves both `dist/` and `/api`, preserving request paths; no separate frontend fallback is needed. Buffering is disabled for SSE; see the [Nginx proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering).

On a fresh system, disable the default site and enable this one:

```bash
sudo unlink /etc/nginx/sites-enabled/default
sudo ln -s /etc/nginx/sites-available/narrify-audio /etc/nginx/sites-enabled/narrify-audio
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
curl -fsS http://127.0.0.1/api/health
```

If other sites already exist, do not remove their default site; use a dedicated domain without `default_server`. Do not recreate existing links. Browse to `http://SERVER_IP/`, or `http://SERVER_IP/#/admin/login` for administrators. Allow required 80/443 ingress through host/cloud firewalls while keeping API 8642, PostgreSQL and Redis on loopback.

### HTTPS

HTTP above is for initial connectivity. For public use, change server_name to your domain, configure DNS and install a certificate, for example:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d audio.example.com
sudo certbot renew --dry-run
```

Replace `audio.example.com`. HTTP validation requires public access to domain port 80; see [Certbot instructions](https://certbot.eff.org/instructions?ws=nginx&os=snap) for other methods. After HTTPS and HTTP redirects work, set `NARRIFY_COOKIE_SECURE=true`, restart API/Workers and log in again via HTTPS. Leave it false for HTTP, otherwise login Cookies cannot work normally. Same-origin deployment needs no permissive CORS; cross-origin deployment also requires appropriate CORS, Cookie and frontend build settings.

## 7. Configure LLM and application capabilities

Log in as the bootstrap administrator, check data services / online Workers, create users/projects and allocate task quota.

Configure an OpenAI-compatible chat/completions endpoint, model and credentials. Remote LLM requires no local CUDA. The endpoint must be reachable from the **Worker host**, not the browser's computer; a client-side localhost address is inappropriate.

The default `http://localhost:11434/v1` does not mean the repository installs/starts Ollama or downloads LLM models. Manage local LLM installation and lifecycle separately; allow adequate VRAM when sharing with TTS.

Script/subtitle parsing, role configuration and BGM matching require corresponding application settings. Supply music-library assets yourself; FFmpeg installation alone cannot provide background music.

The basic dependency subset omits Python audio packages used by chapter merging. Merging existing clips needs FFmpeg and these shared-environment packages, without CUDA or TTS models:

```bash
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install \
  'pydub>=0.25' 'soundfile>=0.12' 'numpy>=2.0'
```

## 8. Full TTS installation

Run this on the target Linux host; the Windows development computer does not need CUDA. With no GPU, defer this section and complete basic acceptance first.

### NVIDIA driver

Confirm a real NVIDIA GPU (GPU passthrough for VMs, a GPU instance for cloud hosting). Install using Ubuntu's driver tooling, reboot and verify. See [Ubuntu NVIDIA drivers](https://ubuntu.com/server/docs/how-to/graphics/install-nvidia-drivers/).

```bash
sudo apt install -y ubuntu-drivers-common
sudo ubuntu-drivers list --gpgpu
sudo ubuntu-drivers install --gpgpu
sudo reboot
```

After logging in again:

```bash
nvidia-smi
```

Expect a detected GPU and working driver. The driver must support the selected PyTorch wheel; nvidia-smi's CUDA version is driver capability, not proof of a CUDA Toolkit installation.

### Python dependencies and CUDA wheels

Stop the application before changing its shared environment. Install full requirements, then a matching CUDA wheel pair. Do not copy Windows' `--no-deps` to Linux; pip must resolve the wheel's NVIDIA runtime dependencies.

```bash
sudo systemctl stop narrify-worker narrify-api
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install \
  -r /opt/narrify-audio/backend/requirements.txt
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install \
  --upgrade --force-reinstall 'torch==2.11.0' 'torchaudio==2.11.0' \
  --index-url https://download.pytorch.org/whl/cu128
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip check
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -c \
  'import torch; from qwen_tts import Qwen3TTSModel; print(torch.__version__, torch.version.cuda); assert torch.cuda.is_available(), "CUDA unavailable"; assert torch.cuda.is_bf16_supported(), "Current worker uses bfloat16 on CUDA"; print(torch.cuda.get_device_name(0))'
```

The [PyTorch historical version table](https://pytorch.org/get-started/previous-versions/#v2110) includes 2.11.0 / CUDA 12.8, matching the repository's Windows installer target. This is not a claim of Linux inference acceptance or the latest release. If hardware/drivers require a different pair, consult upstream support information, record the combination and test real synthesis.

Basic inference with prebuilt wheels needs no full CUDA Toolkit. The current loader does not require FlashAttention or explicitly set flash_attention_2; validate the default implementation first. FlashAttention needs its own architecture, compiler and loader checks.

CUDA loading currently uses bfloat16. An incompatible old GPU requires an implementation adjustment before acceptance. Memory depends on model combinations, batch size and text length; a working driver is insufficient evidence.

### Download models into the service account cache

| Default model | Purpose |
| --- | --- |
| `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | Preset speakers |
| `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | Reference-audio cloning |
| `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` | Voice design from descriptions |

Download as the service account, not into the administrator's cache:

```bash
sudo -u narrify -H env HF_HOME=/var/lib/narrify-audio/huggingface \
  /opt/narrify-audio/.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download

for model in (
    "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "Qwen/Qwen3-TTS-Tokenizer-12Hz",
):
    print(snapshot_download(model))
PY
```

Include the tokenizer to avoid a first-inference download. Initial downloads require connectivity and disk space. The current loader first looks up a model ID in the Hugging Face cache; retain Hub IDs and the complete cache. An arbitrary local directory is not guaranteed as a drop-in replacement. Offline transfers must preserve snapshots, blobs and link relationships; other model sources/directories need loading compatibility verification and possibly code changes.

Ensure the environment's HF_HOME matches the download directory, inspect model/device settings and choose cuda or auto, then restart:

```bash
sudo systemctl start narrify-api narrify-worker
```

Imports and CUDA availability do not prove completion. Synthesize short Chinese text, verify playable audio, then test design, cloning, batch synthesis and merge. The default batch limit is 80; start small on the target GPU rather than assuming that limit is suitable.

## 9. Layered acceptance

| Layer | Check | Passing condition |
| --- | --- | --- |
| Data services | pg_isready, application-role psql, redis-cli ping | Database authentication and queue access |
| Migration | Unit status and logs | upgrade head succeeds |
| API / entrance | Loopback API and Nginx health | Both return ok: true |
| Frontend / authentication | Browser and administrator login | Assets, Cookie session and CSRF work |
| Workers / files | Upload short UTF8 Chinese text; formatting task | Queued → running → completed; readable result |
| SSE | Observe task center | Updates without refreshing; proxy does not close connection |
| LLM | Small script parse | Response, quota settlement and valid output |
| TTS | Short synthesis, design and cloning | Real inference and playable audio |
| Audio | Small merge/split and optional music | Tools available; playable output |

Health does not query the database or verify Workers. Basic tasks can be accepted without TTS; full production also requires LLM and actual audio acceptance.

## Daily operation, logs and upgrades

```bash
# Start: systemd units order dependencies and migrations.
sudo systemctl start narrify-api narrify-worker nginx

# Preserve both service logs and workspace task logs.
sudo journalctl -u narrify-migrate -b --no-pager
sudo journalctl -u narrify-api -u narrify-worker -f

# Wait for tasks before stopping the application; stop dedicated data services only if needed.
sudo systemctl stop narrify-worker narrify-api
sudo systemctl stop redis-server postgresql@16-main
```

After enabling services at boot, an SSH terminal is unnecessary. Restart API/Workers after environment changes; daemon-reload after unit-file changes.

Wait for tasks, stop application writes and back up the database/workspace before upgrading. Check a clean working tree first:

```bash
sudo -u narrify -H git -C /opt/narrify-audio status --short
sudo -u narrify -H git -C /opt/narrify-audio pull --ff-only
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install \
  -r /opt/narrify-audio/backend/requirements.txt
sudo -u narrify -H bash -c 'cd /opt/narrify-audio && npm ci && npm run build'
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip check
sudo systemctl restart narrify-migrate
sudo systemctl start narrify-api narrify-worker
```

After full dependency upgrades recheck CUDA wheels and real inference. Basic-only deployments maintain the basic subset. The migration unit uses RemainAfterExit: upgrading requires an explicit **restart** to rerun it, not merely starting the API.

The one-command build below requires import-linter, absent from current requirements and the TTS installer. Install it before first use:

```bash
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install import-linter
```

With installed systemd units, `bash launch/build.sh` builds and loads local code: frontend typecheck/build, Python compile and import-layer gate, daemon-reload, application stop, data-service start, migration restart, API/Worker startup and health check. It detects system/user mode. It does not pull Git; refresh the browser after completion. Wait for active tasks before service restart.

Dependencies are reused by default. `--install-deps` first stops the application, runs npm ci, installs the complete requirements and checks pip; this includes TTS, so recheck CUDA. `--build-only` builds without systemd operations. It cannot be combined with `--install-deps`. Ordinary build failure leaves services running; dependency-install or migration failure keeps the application stopped until fixed.

On migration failure inspect logs and fix the cause; do not delete/recreate the database. Code rollback and database rollback are distinct; irreversible schema changes require restoring pre-upgrade backups.

## Backups and migration from Windows

The database and files together form project data. Stop application writes before backing up both:

```bash
set -o pipefail
backup_dir="/var/backups/narrify-audio/$(date +%Y%m%d-%H%M%S)"
sudo install -d -o root -g root -m 0700 "$backup_dir"
sudo -u postgres pg_dump -Fc narrify | sudo tee \
  "$backup_dir/narrify-db.dump" >/dev/null

# Skip only optional paths not yet created; treat actual read errors as backup failures.
runtime_paths=()
for item in setting.json app.json config logs .narrify music_library; do
  if sudo test -e "/opt/narrify-audio/$item"; then
    runtime_paths+=("$item")
  fi
done
sudo tar -czf "$backup_dir/narrify-files.tar.gz" \
  -C /var/lib/narrify-audio storage \
  -C /opt/narrify-audio "${runtime_paths[@]}"
```

Use a unique directory per backup, check exit status and output size, and rehearse restoration into an empty database. Separately protect the deployment environment, required Redis persistence and model cache. Models can be downloaded again; business files cannot. General restore and old workspace migration are in [operations](operations.en.md).

For Windows-to-Linux migration:

1. Finish/cancel active work, stop old API/Workers, export PostgreSQL 16 with pg_dump -Fc. Do not copy Windows PostgreSQL's data directory.
2. Install Linux software and `.venv` afresh. Restore into an empty database with pg_restore --no-owner --no-acl as the application owner, then run Alembic. Do not layer a restore over an existing business database.
3. Copy workspace structure, project settings, reference audio and music library; preserve usernames, project IDs and relative paths. Grant narrify read/write access.
4. Inspect database storage.root, repository setting.json and workspace config/setting.json for drive-letter paths, model paths and FFmpeg paths. Replace with Linux equivalents. Do not invoke automatic relocation when the source root is inaccessible; verify restored files/target root and correct configuration while stopped if necessary.
5. Do not copy Windows `.venv`, node_modules, drivers or executables. Check cache links/integrity and case-sensitive file references.
6. Accept basic tasks first, then LLM, GPU inference, cancellation/retry and output downloads. Switch the entrance only after the new host works.

## Troubleshooting and current validation limits

| Symptom | Check |
| --- | --- |
| Nginx 502 | API service, loopback 8642 and migrations |
| Page 503 / missing frontend | npm run build and dist/index.html |
| Tasks remain queued | Worker logs, Redis, Outbox, heartbeat and quota |
| Progress delayed until completion | SSE buffering and proxy timeouts |
| Settings unchanged after restart | Same EnvironmentFile for migration/API/Worker and process restart |
| Storage environment change ignored | Administrator-saved database storage.root |
| Upload 413 | Nginx / upstream limit and NARRIFY_MAX_UPLOAD_BYTES |
| Login failure / writes 403 | HTTPS/Secure Cookie match, readable CSRF Cookie and frontend API URL |
| Write denied / duplicate model downloads | Runtime account, HOME/HF_HOME and permissions |
| nvidia-smi works, torch has no CUDA | CUDA wheel, driver, passthrough and device permissions |
| GPU OOM | Smaller batches; competing LLM/GPU work; do not expand Workers untested |

POSIX interpreter paths, fcntl locks and TTS process-group termination exist in code; Linux automation, inference and production operation still need target-host testing. Some runtime messages suggest install_tts_env.ps1; on Linux follow section 8 instead.

Controlled process management terminates TTS using Windows Job Objects or an independent POSIX session/process group. systemd KillMode=control-group also cleans the whole service on stop. Existing implementation is not target-machine acceptance: test cancellation, pause, retry and temporary-file cleanup before rollout.

Worker lane assignments, write exclusion and machine-wide LLM concurrency are in [development](development.en.md); optional administrator GPU scheduling is in [GPU scheduling](operations.en.md).
