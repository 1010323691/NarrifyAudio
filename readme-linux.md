# Linux 完整部署指南

[English](docs/linux.en.md) · [首页](README.zh-CN.md) · [Windows](docs/windows.md) · [运维](docs/operations.md) · [开发](docs/development.md) · [制作流程](docs/production.md)

本指南面向全新的 **Ubuntu Server 24.04 LTS x86_64**，使用系统软件包、项目 Python 虚拟环境和 systemd 服务部署。Windows 本地部署见 [Windows 部署指南](docs/windows.md)。其他发行版需要调整包名、PostgreSQL 集群名称和服务配置。

安装顺序为：**系统工具 → PostgreSQL / Redis → 项目依赖与前端构建 → 数据库与环境配置 → 迁移 → API / Worker → Nginx → LLM / TTS → 制作验收**。

本文依据当前仓库实现和上游安装资料编写；当前开发机器是 Windows，尚未在 Linux 主机上完成实机验收。尤其是 CUDA、模型推理和任务取消，需要在目标主机验证。

## 与 Windows 部署的区别

| 环节 | Windows 本地方案 | Linux 本指南 |
| --- | --- | --- |
| Python | 正式安装 Python 3.14 | Ubuntu 自带 Python 3.12，创建 `.venv` |
| Node.js | Node.js 24 LTS MSI / winget | NodeSource APT 仓库安装 Node.js 24 |
| 数据库 | PostgreSQL 16 Windows 服务 | APT 安装 PostgreSQL 16，systemd 管理 |
| Redis 协议队列 | Memurai Windows 服务 | APT 安装 Redis，服务名 `redis-server` |
| API / Worker | `launch/start.ps1` 启动本地进程 | 两个独立 systemd 服务 |
| 前端 | 日常使用 Vite `:5173` | 构建 `dist/`，由 API 提供，Nginx 统一入口 |
| 环境变量 | `launch/start.ps1` 读取 `.env` | systemd `EnvironmentFile` 给迁移、API 和 Worker 加载同一配置 |
| TTS | `install_tts_env.ps1` | 在 `.venv` 中安装依赖和 CUDA 版 PyTorch |
| 音频工具 | winget FFmpeg / SoX_ng | APT FFmpeg / SoX，无需 `sox.exe` 兼容副本 |
| 日志 | 进程控制台及应用日志 | journald 加应用 / 任务日志 |

Windows 的 `.ps1` / `.bat` 脚本不适用于 Linux。`launch/` 提供 Bash 启停脚本，系统依赖、数据库和 systemd 服务仍需按本文先行配置；它们不是一键安装脚本。

## Linux 启停脚本

完成下文的 PostgreSQL、Redis、前端构建及三个 `narrify-*` systemd 服务配置后，在仓库根目录执行：

```bash
bash launch/start-data-services.sh # 启动 PostgreSQL、Redis
bash launch/start.sh               # 启动数据服务，应用全停时执行迁移，再启动 API、Worker
bash launch/stop.sh                # 先停 API、Worker，再停数据服务（整体停机）
bash launch/stop-data-services.sh  # 仅停数据服务（要求应用已停）
```

系统级部署中服务操作按需调用 `sudo`，API 和 Worker 仍由 systemd 以 `narrify` 用户运行，使用 `/etc/narrify-audio/narrify.env`；默认数据服务名为 `postgresql@16-main.service`、`redis-server.service`。user 级部署（见下节）不需要 sudo：`launch/` 脚本检测到 `~/.config/systemd/user/` 下五个 `narrify-*.service` 单元齐备后自动改用 `systemctl --user`，数据服务默认名随之变为 `narrify-postgresql.service`、`narrify-redis.service`，日志改用 `journalctl --user -u narrify-api -u narrify-worker` 查看。其他部署形态可通过 `POSTGRES_SERVICE` / `REDIS_SERVICE` 环境变量覆盖数据服务名，并同步调整 systemd 单元的依赖。前端使用构建后的 `dist/` 或 Nginx 入口，正式启动脚本不运行 Vite。

本地开发可使用独立的前台启动器：

```bash
# 先安装 Linux .venv、npm 依赖并配置仓库根目录 .env，再启动数据服务。
# 必须先停掉已有的 API、Worker、Vite，避免重复运行。
bash launch/dev-all.sh
```

开发脚本需要 Bash 4.3+、curl、util-linux 提供的 `flock` / `setsid`，通过 Linux `.venv/bin/python` 将 `.env` 中的 `NARRIFY_*` 作为数据加载，不执行文件中的 shell 代码。它执行迁移，启动 API、Worker 和 Vite（`127.0.0.1:5173`）；后台日志写入 `logs/dev/`。重复启动会被锁拒绝，任何组件退出会结束整套开发进程。按 `Ctrl+C` 停止脚本启动的进程，最多等待 10 秒后强制结束；数据服务继续运行。`dev-all.sh` 运行期间，数据服务停止脚本会拒绝停止数据库和 Redis。

脚本统一使用 LF 换行，默认通过 `bash launch/脚本名.sh` 调用，也可直接执行。使用 `sh` 调用入口脚本时会自动切换到 Bash；进入 `launch/` 目录后也可运行 `bash start.sh`。

## 用户级部署（无 sudo / 无系统软件包）

这是高级部署形态说明，本文未提供自编译软件和五个 user 单元的完整安装模板；下文首次安装采用系统级部署。无 sudo 权限、或不希望安装系统软件包时，可在 `~/.local` 下自编译安装 PostgreSQL 16 与 Redis，并在 `~/.config/systemd/user/` 安装五个 user 级单元：`narrify-postgresql`、`narrify-redis`、`narrify-migrate`（oneshot 迁移）、`narrify-api`、`narrify-worker`。

- `launch/` 脚本自动检测该形态：五个单元**全部存在**时，所有单元操作走 `systemctl --user`（不 sudo），数据服务默认名切换为 `narrify-postgresql.service` / `narrify-redis.service`；任一缺失则回退系统级行为（`sudo systemctl` + 发行版默认服务名）。
- 环境变量仍由单元的 `EnvironmentFile` 供给（如仓库根目录 `.env`），脚本不 source `.env`。
- 日志：`journalctl --user -u narrify-api -u narrify-worker`。
- user 级服务随用户会话存活：登出且所有会话结束即停止；需要登出后保活时执行 `sudo loginctl enable-linger <用户>`（需有 sudo）。

## 最终运行链路

```mermaid
flowchart LR
  Browser[浏览器] <-->|HTTP / HTTPS| Nginx[Nginx :80 / :443]
  Nginx <-->|页面、API、SSE| API[FastAPI 127.0.0.1:8642]
  Dist[前端构建 dist/] --> API
  API -->|业务记录与任务 Outbox| PG[PostgreSQL 16 :5432]
  API <--> Redis[Redis :6379]
  Worker[Worker 池 4 机械 + 4 模型] <-->|领取任务、保存结果| PG
  Worker <--> Redis
  Worker --> Storage[持久化工作空间]
  Worker --> LLM[可访问的 LLM API]
  Worker --> TTS[隔离 TTS 子进程]
  TTS --> GPU[NVIDIA GPU / PyTorch / 模型]
  Worker --> Audio[FFmpeg / ffprobe / SoX]
```

前端构建完成后，无需长期运行 Vite 或 Node.js 服务。常驻进程为 **PostgreSQL、Redis、API、Worker 和 Nginx**；使用本地 LLM 时再独立管理 LLM 服务。TTS 由 Worker 按任务拉起，不需要另外启动 Qwen 网页演示或 TTS HTTP 服务。

基础应用运行不要求 CUDA；完整的剧本解析、角色配音、语音合成与音频制作，还需要可用的 LLM、TTS 依赖、模型和音频工具。没有 NVIDIA GPU 时可以先验收基础应用；代码支持 CPU 设备，但 CPU 推理速度、内存需求和实际兼容性需要另测。

## 1. 安装系统组件

以下命令在 Linux 的 Bash 中执行，使用有 sudo 权限的登录账号。逐步执行并确认成功；遇到失败先修复，再继续下一步。不要在 Windows PowerShell 中执行。

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential pkg-config \
  python3 python3-venv python3-pip \
  postgresql-16 postgresql-client-16 redis-server nginx \
  ffmpeg sox libsox-fmt-all libsndfile1
```

Ubuntu 24.04 的发行版组件包含 PostgreSQL 16；指定 `postgresql-16`，避免在其他系统上不经确认安装不同数据库大版本。[Ubuntu 24.04 发行说明](https://documentation.ubuntu.com/release-notes/24.04/)

安装 Node.js 24 的 DEB 软件包。这里使用 NodeSource 软件包仓库，下载脚本用于配置 APT 源；不使用便携 Node 或 nvm。[NodeSource 安装说明](https://github.com/nodesource/distributions/blob/master/DEV_README.md)

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

预期 Python 为 3.12、Node.js 为 24、PostgreSQL 为 16。这是本文目标组合，不表示最新版本或全部机器均已验证。Linux 方案选择 Python 3.12，与 [Qwen3-TTS 官方环境建议](https://github.com/QwenLM/Qwen3-TTS#environment-setup)一致；不必照搬 Windows 的 Python 版本或安装 Conda。

## 2. 配置数据服务

```bash
sudo systemctl enable --now postgresql redis-server
pg_lsclusters
pg_isready -h 127.0.0.1 -p 5432
redis-cli -h 127.0.0.1 ping
```

预期 PostgreSQL 集群 `16/main` 为 `online`，Redis 返回 `PONG`。本指南使用 Ubuntu 默认集群；真正的 PostgreSQL 进程由 `postgresql@16-main.service` 管理，`postgresql.service` 是集群总入口。

确认 PostgreSQL 只监听回环地址，Redis 的 `/etc/redis/redis.conf` 保持回环 `bind` 和 `protected-mode yes`。数据库和队列无需对浏览器开放，不向公网开放 5432、6379。

Redis 的任务流需要持久化。用 `sudoedit /etc/redis/redis.conf` 将现有 `appendonly` 配置改为 `yes`，保留 `appendfsync everysec`，然后执行：

```bash
sudo systemctl restart redis-server
redis-cli CONFIG GET appendonly
redis-cli CONFIG GET appendfsync
```

创建专用数据库用户，使用交互式密码设置，避免把密码写入 shell 命令：

```bash
sudo -u postgres createuser --no-superuser --no-createdb --no-createrole narrify
sudo -u postgres psql
```

在 psql 内执行，然后退出：

```text
\password narrify
\q
```

创建 UTF8 数据库，并验证 TCP 密码认证：

```bash
sudo -u postgres createdb --owner=narrify --encoding=UTF8 --template=template0 narrify
psql -h 127.0.0.1 -U narrify -d narrify -W -c 'SHOW server_encoding;'
```

密码建议使用足够长的随机字母和数字，数据库连接 URL 可以直接使用；如果包含 `@`、`:`、`/`、`#`、`%` 等字符，填入连接 URL 时必须对密码部分做百分号编码。SQL 登录用原始密码。若 TCP 认证失败，检查 `/etc/postgresql/16/main/pg_hba.conf` 的回环 `host` 规则采用 `scram-sha-256`，不要改成 `trust`。[Ubuntu PostgreSQL 安装说明](https://documentation.ubuntu.com/server/how-to/databases/install-postgresql/index.html)

这些创建命令用于新电脑；已有角色和数据库时先核对配置，不重复创建或删除。

## 3. 创建运行账号、拉取代码并构建

约定以下目录，便于后续服务配置直接复用：

| 路径 | 内容 |
| --- | --- |
| `/opt/narrify-audio` | 源码、`.venv`、`dist/` 及仓库根目录运行时文件 |
| `/etc/narrify-audio/narrify.env` | 数据库、队列、初始管理员和部署环境变量 |
| `/var/lib/narrify-audio/storage` | 用户和项目工作空间 |
| `/var/lib/narrify-audio/huggingface` | 服务账号共用的模型缓存 |
| `/var/backups/narrify-audio` | 数据库与文件备份 |

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

先安装基础依赖，完成数据库和任务链路后再安装 TTS：

```bash
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install \
  'fastapi>=0.115' 'uvicorn>=0.30' 'pydantic>=2.8' 'python-multipart>=0.0.9' \
  'SQLAlchemy>=2.0.36' 'alembic>=1.14' 'psycopg[binary]>=3.2' \
  'redis>=5.2' 'psutil>=6.0'
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip check
```

这份基础清单对应当前后端导入需求。`backend/requirements.txt` 同时包含 TTS 和测试依赖，完整安装放在第 8 节；将来仓库依赖变化时，应同步核对本清单。

构建前端：

```bash
sudo -u narrify -H bash -c 'cd /opt/narrify-audio && npm ci && npm run build'
```

采用同源部署，前端使用默认相对 `/api` 地址，**不设置 `VITE_API_BASE`**。尤其不要把服务器的 `127.0.0.1:8642` 写进前端配置，否则远程浏览器会访问客户端自己的回环地址。构建前核对已有 `.env*` 是否残留分域配置。

API、Worker 和 TTS 共用 `.venv`，TTS 仍保持子进程隔离。当前应用可能写入仓库根目录的 `setting.json`、`config/`、`logs/`、`.narrify/` 和 `music_library/`，因此本方案让运行账号拥有项目目录；不能直接将整个 `/opt/narrify-audio` 挂载为只读。

## 4. 保存部署环境配置

```bash
sudo install -o root -g narrify -m 0640 /dev/null /etc/narrify-audio/narrify.env
sudoedit /etc/narrify-audio/narrify.env
```

填入以下内容，替换所有密码占位值：

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

这是 systemd 环境文件，使用逐行 `KEY=value`，不要写 `export`、命令替换或引用其他变量。密码占位值必须改成真实值；不要提交此文件。API 和 Worker 不会自动加载仓库 `.env`，手动执行 Python 命令也不会获得这里的部署变量。

本例关闭公开注册，可在基础验收后按实际需求调整。初始管理员在 API 首次启动时创建；已有同邮箱账号时不会重置密码。LLM 凭据、模型与制作参数通过管理控制台配置，不写入此环境文件。

`NARRIFY_STORAGE_ROOT` 是部署默认值；管理控制台保存的数据库配置 `storage.root` 会优先于它生效。新电脑按上述路径部署；迁移旧数据库时尤其需要检查这一项。

## 5. 用 systemd 执行迁移并管理进程

### 数据库连接预算与故障恢复

连接池按进程分别配置，默认 API 业务池与迁移锁池均为 `16+8`，每个 Worker
业务池为 `3+1`、锁池为 `1+0`（未使用时不创建连接）。一个 API 进程加八个
Worker（4 个机械任务 + 4 个模型任务）的连接上限为 `48 + 8×5 = 88`；PostgreSQL 保持 `max_connections=100`
时，剩余 12 个连接供迁移、运维及其他客户端使用。增加 API/Worker 进程或
解析并发之前，须重新计算所有进程的池上限，而非只增大单个连接池。

`.env.example` 列出 `NARRIFY_API_DB_*` 和 `NARRIFY_WORKER_DB_*` 配置；
`POOL_SIZE` 必须大于零，`POOL_MAX_OVERFLOW` 可为零，禁止无限溢出。
同一组的 `POOL_TIMEOUT` 可指定等待秒数：API 业务池默认 60、锁池 30，
Worker 两个池均为 10。修改后重启相应进程生效。标准
`python -m backend.worker` 自动选择 Worker 配置；自定义嵌入式启动器可设置
`NARRIFY_DB_ROLE=worker`，API 则使用 `api`，不要在共享环境文件中固定此角色。

持续运行的 Worker 在启动心跳、心跳更新、取任务或调度遇到数据库连接中断、
连接耗尽或连接池超时时，以 1、2、4 秒递增退避，最长间隔 30 秒，数据库恢复
后继续工作。已有任务仍由租约与 fencing 机制保护，不直接重放执行。
错误心跳和退出状态上报失败不会覆盖原始异常。`--once` 保持失败即退出，
错误凭据、SQL 编程错误等非瞬时故障不会无限重试。

分别用 `sudoedit` 创建下面三个文件。API 与 Worker 使用同一工作目录、解释器和环境文件；数据库迁移只由独立的一次性服务执行。

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

若 PostgreSQL 使用了其他集群名称，修改迁移服务的 `Requires` 与 `After`。不要用 `NARRIFY_AUTO_CREATE_SCHEMA=true` 替代 Alembic。

加载配置并首次启动：

```bash
sudo systemctl daemon-reload
sudo systemctl start narrify-migrate
sudo systemctl status narrify-migrate --no-pager
sudo systemctl enable --now narrify-api narrify-worker
sudo systemctl status narrify-api narrify-worker --no-pager
curl -fsS http://127.0.0.1:8642/api/health
```

迁移成功后 `narrify-migrate` 显示 `active (exited)` 是正常状态。API 固定监听 `127.0.0.1:8642`，浏览器入口由 Nginx 提供。先部署一个 API 进程和一个 Worker 池服务；池内默认 4 个机械 Worker + 4 个模型 Worker。扩容前须核对连接预算、GPU 显存和模型许可限制，见 [开发指南](docs/development.md)。

## 6. 配置 Nginx 同源入口

用 `sudoedit /etc/nginx/sites-available/narrify-audio` 保存：

```nginx
server {
    listen 80 default_server;
    server_name _;

    # 后端默认文件上限 50 MiB；这里给 multipart 请求留出额外空间。
    client_max_body_size 64m;

    location / {
        proxy_pass http://127.0.0.1:8642;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";

        # 任务事件使用 SSE，需要及时向浏览器传递响应。
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

本例由 API 同时提供 `dist/` 和 `/api`，所有请求都保留原始路径，无需额外配置前端路由回退。关闭响应缓冲用于 SSE 事件传递。[Nginx 代理模块说明](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering)

在全新系统中禁用默认站点，再启用本项目：

```bash
sudo unlink /etc/nginx/sites-enabled/default
sudo ln -s /etc/nginx/sites-available/narrify-audio /etc/nginx/sites-enabled/narrify-audio
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
curl -fsS http://127.0.0.1/api/health
```

若服务器已有其他站点，不执行默认站点删除命令；给本项目配置独立域名，并去掉 `default_server`。已经存在的链接也无需重复创建。

在浏览器打开 `http://服务器IP/`，管理员登录入口为 `http://服务器IP/#/admin/login`。云主机安全组及本机防火墙允许所需的 80 / 443；API 8642、PostgreSQL 5432 和 Redis 6379 保持回环访问。

### HTTPS

上面的 HTTP 配置用于初次连通验收。面向公网使用时，先将 `server_name _;` 改成自己的域名，配置 DNS 指向服务器，然后安装证书。例如采用 Certbot 的 Nginx 插件：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d audio.example.com
sudo certbot renew --dry-run
```

将 `audio.example.com` 替换为真实域名；Nginx 插件的 HTTP 验证需要域名能从公网访问 80 端口。其他安装方式或 DNS 验证见 [Certbot 官方指南](https://certbot.eff.org/instructions?ws=nginx&os=snap)。

HTTPS 生效并启用 HTTP 到 HTTPS 重定向后，把环境文件中的 `NARRIFY_COOKIE_SECURE` 改为 `true`，重启 API 和 Worker，再用 HTTPS 重新登录。仅使用 HTTP 时不要设为 `true`，否则浏览器不会正常携带登录 Cookie。同源部署无需放开 CORS；若以后改成前后端分域，需要另配 CORS、Cookie 和前端构建变量。

## 7. 配置 LLM 和应用能力

先用初始管理员账号登录，检查数据服务和在线 Worker，按需要创建用户、项目并分配任务额度。

在管理控制台设置可访问的 OpenAI-compatible `chat/completions` 服务地址、模型名称与凭据。可以使用远程 LLM，或另行部署本地 LLM 服务；远程 LLM 不要求本机 CUDA。地址必须从 **Worker 所在服务器** 能访问，不能填客户端电脑的 `localhost`。

项目默认地址是 `http://localhost:11434/v1`，但这不表示仓库会安装或启动 Ollama，也不会自动准备 LLM 模型。本地 LLM 的安装和常驻进程需单独管理；若与 TTS 共用 GPU，必须给两者留足显存。

字幕 / 剧本解析、角色配置和背景音乐匹配等能力需要对应的应用配置。音乐库文件也需要自行准备；没有素材时不能仅靠安装 FFmpeg 完成背景音乐制作。

基础依赖清单未包含章节合并所需的 Python 音频包。仅合并已有片段时，无需安装 CUDA/TTS 模型，但需 FFmpeg 及以下共享环境依赖：

```bash
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install \
  'pydub>=0.25' 'soundfile>=0.12' 'numpy>=2.0'
```

## 8. 完整 TTS 部署

此节用于目标 Linux 主机上的完整语音制作；不要求当前 Windows 电脑安装 CUDA。没有 GPU 时可暂缓，先完成第 9 节的基础验收。

### NVIDIA 驱动

确认主机实际拥有可用的 NVIDIA GPU；虚拟机需要 GPU 直通，云主机需要 GPU 实例。驱动可通过 Ubuntu 推荐方式安装，重启后验证：[Ubuntu NVIDIA 驱动指南](https://ubuntu.com/server/docs/how-to/graphics/install-nvidia-drivers/)

```bash
sudo apt install -y ubuntu-drivers-common
sudo ubuntu-drivers list --gpgpu
sudo ubuntu-drivers install --gpgpu
sudo reboot
```

重新登录后执行：

```bash
nvidia-smi
```

需要看到 GPU 和正常工作的驱动。驱动必须支持所选 PyTorch CUDA wheel；`nvidia-smi` 显示的 CUDA 版本表示驱动支持能力，不代表已安装 CUDA Toolkit。

### 安装 Python 依赖和 CUDA 版 PyTorch

停止应用后安装当前仓库完整依赖，再安装明确的 CUDA wheel。Linux 不要照搬 Windows 脚本的 `--no-deps`，让 pip 安装 wheel 所需的 NVIDIA 运行时依赖。

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

上述 PyTorch 2.11 / CUDA 12.8 组合对应仓库 Windows 安装脚本，也在 [PyTorch 官方版本安装表](https://pytorch.org/get-started/previous-versions/#v2110)列出 Linux wheel；这不是本项目已完成 Linux 推理验收的声明。硬件或驱动不适用时，按官方支持表选择成对匹配的 torch / torchaudio 版本，记录实际组合，再做真实合成测试。

使用预编译 wheel 的基本推理不需要额外安装完整 CUDA Toolkit。当前 TTS 加载器没有要求 FlashAttention，也没有显式设置 `attn_implementation="flash_attention_2"`；先完成默认实现的验收。后续启用 FlashAttention 涉及 GPU 架构、编译工具和加载配置，应单独处理。

当前加载器对 CUDA 固定使用 `bfloat16`，所选 GPU 必须支持这一精度；旧 GPU 若不支持，需要调整推理实现后再验收。显存需求由模型组合、批量和文本长度决定，不能只根据驱动安装成功判断设备适用。

### 下载模型到服务账号共用缓存

当前默认配置使用以下三个模型：

| 模型 | 用途 |
| --- | --- |
| `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | 预设音色合成 |
| `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | 参考音频克隆 |
| `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` | 描述生成音色 |

从服务账号下载，而不是下载到管理员自己的 `~/.cache`：

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

同时准备 tokenizer 缓存，避免首次推理再临时下载。模型首次下载需要网络和足够磁盘空间。当前加载器先用模型 ID 查询 Hugging Face 缓存，因此本文保留默认 Hub ID 和完整缓存；不要仅把模型目录绝对路径填入设置并假定可用。离线搬迁缓存时要保留 snapshots、blobs 和符号链接的对应关系；改用其他模型源或普通本地目录需要另做加载兼容性验证，必要时调整加载器。

确认环境文件中的 `HF_HOME` 与下载位置一致，在应用设置中检查模型和设备，使用 `cuda` 或 `auto`，再启动服务：

```bash
sudo systemctl start narrify-api narrify-worker
```

不要仅凭导入成功或 `torch.cuda.is_available()` 判定完成。使用短中文文本真实合成，确认输出音频可播放，再分别验收音色设计、参考音频克隆、批量合成和音频合并。当前默认批量上限为 80，应先用小批量验证目标 GPU；不同模型和文本长度的显存需求不同。

## 9. 逐层验收

| 层级 | 验收方式 | 通过条件 |
| --- | --- | --- |
| 数据服务 | `pg_isready`、应用用户 `psql`、`redis-cli ping` | 数据库可认证、队列可访问 |
| 数据库迁移 | `systemctl status narrify-migrate`、迁移日志 | `upgrade head` 成功 |
| API 与入口 | 回环 API 和 Nginx `/api/health` | 均返回 `ok: true` |
| 前端与认证 | 浏览器打开页面并登录管理员 | 静态资源可加载，Cookie session / CSRF 正常 |
| Worker 与文件 | 上传 UTF8 中文短文本，提交排版任务 | 待处理 → 执行 → 完成，生成结果可读取 |
| SSE | 浏览器任务中心观察进度和结束事件 | 无需手动刷新即可更新，连接不中途被代理关闭 |
| LLM | 小段剧本解析，检查任务与输出 | 模型响应、额度结算和结果正常 |
| TTS | 短文本合成、设计、克隆 | 模型实际推理成功，音频可播放 |
| 音频制作 | 小章节合并、切分，按需混入音乐 | FFmpeg / ffprobe / SoX 可用，成品可播放 |

`/api/health` 不查询数据库，也不验证 Worker，HTTP 200 不能代替真实任务验收。基础任务可在未安装 TTS 时先做；完整部署必须继续完成 LLM 和实际音频制作验收。

## 日常启停、日志和升级

```bash
# 启动：依赖服务和迁移由 systemd 单元按顺序处理。
sudo systemctl start narrify-api narrify-worker nginx

# 日志：服务日志与工作空间内任务日志都应保留。
sudo journalctl -u narrify-migrate -b --no-pager
sudo journalctl -u narrify-api -u narrify-worker -f

# 停止：先等待制作任务结束，再停止应用；需要时才停止专用数据服务。
sudo systemctl stop narrify-worker narrify-api
sudo systemctl stop redis-server postgresql@16-main
```

API、Worker 和数据服务开机启动配置完成后，无需 SSH 登录后手动打开终端。修改环境文件后重启 API 和 Worker；修改单元文件后先执行 `systemctl daemon-reload`。

升级前等待任务结束、停止 API / Worker，同时备份数据库和工作空间。确认工作树干净后再更新：

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

完整依赖升级后重新核对 CUDA wheel 与 GPU 推理；仅使用基础环境的部署继续按基础依赖清单维护。`narrify-migrate` 设置了 `RemainAfterExit`，升级时必须显式 **restart** 才会再次执行迁移，不能只重新启动 API。

以下构建入口包含 import-linter 门禁，而当前 requirements 和 TTS 安装脚本不安装该工具。首次使用前执行：

```bash
sudo -u narrify -H /opt/narrify-audio/.venv/bin/python -m pip install import-linter
```

已安装 systemd 单元的机器，更新本地源码后可在仓库根目录执行 `bash launch/build.sh`，一键构建并加载最新代码。脚本自动识别 user / system 部署，先完成前端类型检查与构建、Python 编译检查和分层门禁，再执行 `daemon-reload`、停止 API / Worker、启动数据服务、重新运行迁移、启动 API / Worker，并检查 API 健康状态。它不拉取 Git 更新；浏览器完成后刷新。执行期间已有任务会受到服务重启影响，建议等待制作任务结束再运行。

默认复用已安装依赖；依赖清单有变化时运行 `bash launch/build.sh --install-deps`，会先停止应用，再执行 `npm ci`、安装 `backend/requirements.txt` 和 `pip check`。这包括 TTS 依赖，完整依赖升级后的 CUDA 核对仍按上文执行。只需构建产物时使用 `bash launch/build.sh --build-only`，无需 systemd。普通构建失败时不停止服务；依赖安装模式中的失败及迁移失败会保持应用停止，修复后重新运行脚本。

迁移失败时保持应用停止，查看日志并修复原因，不删库重建。代码回退与数据库回退是两件事；不可逆迁移依靠升级前备份恢复。

## 备份与 Windows 数据迁移

数据库和文件共同构成项目数据。先停止应用写入，再同时备份：

```bash
set -o pipefail
backup_dir="/var/backups/narrify-audio/$(date +%Y%m%d-%H%M%S)"
sudo install -d -o root -g root -m 0700 "$backup_dir"
sudo -u postgres pg_dump -Fc narrify | sudo tee \
  "$backup_dir/narrify-db.dump" >/dev/null

# 只略过尚未创建的可选目录，实际读取错误仍应视为备份失败。
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

使用每次唯一的备份目录，检查命令退出状态、导出大小，并定期在空数据库中演练恢复。另行安全保存部署环境文件、必要的 Redis 持久化文件及模型缓存；模型可重新下载，但业务文件不能。

从 Windows 迁移时：

1. 停止旧 API / Worker，让在途任务结束或取消，使用 PostgreSQL 16 的 `pg_dump -Fc` 导出数据库；不要复制 Windows PostgreSQL 数据目录。
2. Linux 重新安装软件和 `.venv`，在空数据库中通过 `pg_restore --no-owner --no-acl` 恢复，由应用用户拥有表，再运行 Alembic。不要把恢复操作叠加到已有业务库。
3. 原样复制工作空间目录结构、项目配置、参考音频及音乐库，保留用户名、项目 ID 和相对路径，并让 `narrify` 可读写。
4. 检查数据库 `storage.root`、根目录 `setting.json` 和工作空间 `config/setting.json` 中的工作目录、模型路径、FFmpeg 路径等，将 Windows 盘符路径调整为 Linux 路径。旧根路径无效时，不直接调用依赖旧目录仍可访问的自动搬迁；先核对恢复出的文件与目标路径，必要时在停机状态修正配置。
5. 不复制 Windows `.venv`、`node_modules`、驱动或 `.exe` 工具；模型缓存也需检查链接和文件完整性。Linux 路径区分大小写，大小写不一致的文件引用需要修复。
6. 先做基础任务验收，再测试 LLM、GPU 推理、取消 / 重试和成品下载，确认新机可用后再切换访问入口。

## 常见问题与当前限制

| 现象 | 检查方向 |
| --- | --- |
| Nginx 502 | `narrify-api` 是否运行，8642 回环是否可访问，迁移是否成功 |
| 页面 503 / 找不到前端 | 是否执行 `npm run build`，项目目录下是否有 `dist/index.html` |
| 页面可打开但任务一直等待 | Worker 日志、Redis、Outbox、Worker 心跳与任务额度 |
| 任务进度延迟、只在结束时刷新 | Nginx 及上游网关是否缓冲 SSE，代理超时是否足够 |
| 重启后配置无效 | 三个单元是否使用同一 `EnvironmentFile`，是否重启进程 |
| 修改存储环境变量后仍访问旧目录 | 管理员保存的数据库 `storage.root` 是否覆盖部署默认值 |
| 上传 413 | Nginx / 上游网关限制与后端 `NARRIFY_MAX_UPLOAD_BYTES` |
| 登录失败或写操作 403 | HTTPS 与 Secure Cookie 是否匹配，CSRF Cookie 是否可读，是否残留错误的前端 API 地址 |
| 无法写入或模型重复下载 | 服务账号权限、HOME、HF_HOME，是否下载到了另一账号的缓存 |
| `nvidia-smi` 正常但 torch 无 CUDA | wheel 是否为 CUDA 版本、驱动兼容性、GPU 直通与设备权限 |
| GPU OOM | 降低批量，检查同时运行的 LLM / 其他 GPU 任务，避免未经验证启动多个 Worker |

当前代码已处理 POSIX 虚拟环境路径、`fcntl` 文件锁以及 TTS 子进程组终止，但 Linux 自动化部署、GPU 冒烟和生产运行仍需目标机验证。部分 TTS 错误提示还会建议运行 `install_tts_env.ps1`，Linux 实际按本文第 8 节安装。

TTS 强制终止由受控进程管理器处理：Windows 使用 Job Object，POSIX 创建独立会话并终止进程组。systemd 的 `KillMode=control-group` 还会在停止整个 Worker 服务时清理服务组。实现存在不等于目标机已验收；上线仍须覆盖取消、暂停、重试和临时文件清理。

Worker 池分工、任务互斥和全机 LLM 请求并发见 [开发指南](docs/development.md)；管理员可选 GPU 调度见 [GPU 调度](docs/operations.md)。
