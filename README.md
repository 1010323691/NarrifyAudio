# NarrifyAudio

NarrifyAudio 是 Windows 本地有声书制作工作台。用户端围绕项目和制作流程运行；管理控制台单独提供平台管理入口。应用由 Vue 3 前端、FastAPI API、后台 Worker、PostgreSQL 和 Redis 协议兼容缓存服务组成。

本指南面向 Windows 10/11 原生开发环境：PostgreSQL 与 Memurai 作为 Windows 服务运行，API、Worker 和前端作为本地进程运行。

## 服务与本机地址

| 服务 | 用途 | 地址 / 端口 | 启动方式 |
| --- | --- | --- | --- |
| PostgreSQL 16 | 用户、项目、任务和配置数据 | `127.0.0.1:5432` | Windows 服务 `postgresql-x64-16` |
| Memurai Developer | Redis 协议缓存和任务队列 | `127.0.0.1:6379` | Windows 服务 `Memurai` |
| FastAPI | 应用 API | `http://127.0.0.1:8642` | `.venv` 中的 Python |
| Worker | 执行后台制作任务 | 与 API 共用 `.venv` | `.venv` 中的 Python |
| Vite | 用户端和管理控制台 | `http://127.0.0.1:5173` | Node.js / npm |

## 首次安装

### 1. 安装依赖

安装 Git、Node.js（建议 20 LTS）和 Python 3.14。用 Windows 原生安装程序安装 PostgreSQL 16（安装时记下 `postgres` 数据库管理员密码）；Memurai Developer 可用 winget 安装。然后在仓库根目录打开 PowerShell：

```powershell
npm.cmd install
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\backend\requirements.txt
winget install --id PostgreSQL.PostgreSQL.16 --exact
winget install --id Memurai.MemuraiDeveloper --exact
```

PostgreSQL 安装程序会要求设置 `postgres` 密码；请记下该密码并按下一节填写 `.env`。如果 winget 没有提供交互式安装选项，可使用 PostgreSQL 官方 Windows 安装程序完成安装。

首次使用 TTS 合成、角色配音或音频处理前，安装共享 TTS 环境（依赖体积较大）：

```powershell
.\install_tts_env.ps1 -PythonVersion 3.14
```

如真实 TTS 冒烟运行不兼容 Python 3.14，可按脚本说明改用 Python 3.10 重建环境：

```powershell
.\install_tts_env.ps1 -PythonVersion 3.10 -Recreate
```

安装 FFmpeg 并确保 `ffmpeg` 和 `ffprobe` 可从 PATH 找到。TTS 音频工具需要 SoX_ng；启动脚本会在检测到 winget 安装的 SoX_ng 时，建立工作区内的 `sox.exe` 兼容副本。

### 2. 配置环境变量和数据库

复制示例配置并编辑本机 `.env`：

```powershell
Copy-Item .env.example .env
notepad .env
```

`NARRIFY_DATABASE_URL` 中 `narrify` 后面的密码是应用专用数据库用户的密码（安装 PostgreSQL 时的 `postgres` 管理员密码仅供人工 psql 运维使用，应用不读取、无需存入 `.env`）。生成强密码并在连接串中进行 URL 编码（例如 `@` 写成 `%40`）。`.env` 已被 Git 忽略，不要提交实际密码。

以 PostgreSQL 管理员创建应用用户和数据库。将下方密码替换为 `.env` 中连接串对应的应用密码：

```powershell
$env:PGPASSWORD = '你的 postgres 管理员密码'
psql -h 127.0.0.1 -U postgres -d postgres -c "CREATE ROLE narrify LOGIN PASSWORD '你的应用数据库密码';"
psql -h 127.0.0.1 -U postgres -d postgres -c "CREATE DATABASE narrify OWNER narrify;"
Remove-Item Env:PGPASSWORD
```

若数据库角色或数据库已存在，不要重复执行对应的 `CREATE` 命令。确认 `.env` 的数据库 URL 指向该数据库，例如：

```text
NARRIFY_DATABASE_URL=postgresql+psycopg://narrify:应用数据库密码@127.0.0.1:5432/narrify
NARRIFY_REDIS_URL=redis://127.0.0.1:6379/0
```

首次启动会运行 Alembic 数据库迁移，并按 `.env` 中的 `NARRIFY_BOOTSTRAP_ADMIN_EMAIL` 和 `NARRIFY_BOOTSTRAP_ADMIN_PASSWORD` 创建管理员账号（如果尚不存在）。不要把生产密码写进 README 或提交 `.env`。

## 启动和关闭

### 启动 PostgreSQL 与 Memurai

在仓库根目录的 PowerShell 执行：

```powershell
.\start-data-services.ps1
```

也可以双击 `start-data-services.bat`；如果当前权限不足，Windows 会弹出管理员授权提示。

脚本会启动 Windows 服务 `postgresql-x64-16` 与 `Memurai`，并等待 `5432`、`6379` 端口就绪。若服务尚未安装，请先安装 PostgreSQL 16 和 Memurai Developer。Windows 可能要求以管理员身份运行 PowerShell 来控制服务。

### 启动整个应用

数据服务就绪后，双击 `start.bat`，或运行：

```powershell
.\start.ps1
```

该脚本会检查 PostgreSQL 和 Memurai，运行数据库迁移，按需启动 FastAPI、Worker 和 Vite，并在服务就绪后打开登录页。每个应用进程会有单独的控制台窗口。脚本可重复运行，已启动的进程会被复用。

- 用户端：<http://127.0.0.1:5173/#/login>
- 管理员登录：<http://127.0.0.1:5173/#/admin/login>
- API 健康检查：<http://127.0.0.1:8642/api/health>

用户与管理员使用各自账号登录；管理控制台入口和可执行操作由账号权限决定。管理员初始账号由 `.env` 的 bootstrap 配置确定，不应依赖 README 中的固定凭据。

### 关闭服务

先关闭 `start.ps1` 打开的 Backend、Worker 和 Frontend 控制台窗口（输入 `Ctrl+C` 并等待进程退出，或关闭窗口）。之后在仓库根目录运行：

```powershell
.\stop-data-services.ps1
```

也可以双击 `stop-data-services.bat`；如果当前权限不足，Windows 会弹出管理员授权提示。

脚本会停止 Memurai 和 PostgreSQL Windows 服务。停止数据服务前先停止 API 与 Worker，避免正在运行的请求或任务因数据库、队列断开而失败。再次启动时先双击 `start-data-services.bat`，再运行 `start.bat`。

## 目录和关键配置

- `src/`：Vue 页面、组件、API 客户端和状态管理。
- `backend/`：FastAPI 路由、业务逻辑、数据库模型和 Worker。
- `tts-engine/`：隔离运行的 TTS 子进程代码。
- `backend/resources/`：提示词和运行资源。
- `.env`：本机数据库、队列和初始管理员配置；不要提交。
- `storage/`、`config/`、`logs/`：运行时用户数据和配置；不要当作源码清理。

常用 `.env` 配置：

| 变量 | 说明 |
| --- | --- |
| `NARRIFY_DATABASE_URL` | 应用数据库连接串 |
| `NARRIFY_REDIS_URL` | Memurai/Redis 连接串 |
| `NARRIFY_BOOTSTRAP_ADMIN_EMAIL` | 首次初始化管理员邮箱 |
| `NARRIFY_BOOTSTRAP_ADMIN_PASSWORD` | 首次初始化管理员密码 |

`postgres` 管理员密码仅供人工 psql 运维使用，应用不读取、无需存入 `.env`。

LLM 服务凭据和制作参数按应用设置页面配置。不要把 API 密钥写入 README、提交到 Git 或放进前端代码。

## 常见问题

### PostgreSQL 或 Memurai 没有启动

运行 `Get-Service postgresql-x64-16, Memurai` 查看服务状态；启动时若遇到权限错误，以管理员身份打开 PowerShell，再运行 `start-data-services.ps1`。服务日志可从 Windows 事件查看器及各自安装目录排查。

### 端口已被占用

```powershell
Get-NetTCPConnection -State Listen -LocalPort 5173,5432,6379,8642 -ErrorAction SilentlyContinue
```

确认占用端口的是 NarrifyAudio 对应服务。不要同时启动另一套 WSL 或 PostgreSQL/Redis 实例占用相同端口。

### 数据库认证失败

核对 PostgreSQL 的 `postgres` 安装密码、`.env` 中的 `NARRIFY_DATABASE_URL`、应用用户密码以及 `narrify` 数据库是否已创建。数据库连接串中的特殊字符需要 URL 编码。修改 `.env` 后，重新启动 API 进程使配置生效。

### API、Worker 或前端未就绪

查看对应控制台窗口中的首个错误。确认已安装 `.venv`、`node_modules` 和 `.env`，PostgreSQL/Memurai 端口正常，并且 `5173`、`8642` 没有被旧进程占用。

## 开发命令

```powershell
npm.cmd run dev                 # 仅启动前端
.\.venv\Scripts\python.exe -m backend.main   # 仅启动 API
.\.venv\Scripts\python.exe -m backend.worker # 仅启动 Worker
npm.cmd run typecheck           # 前端类型检查
npm.cmd run build               # 类型检查并构建前端
npm.cmd run build:all           # 前端构建 + 后端编译检查 + 分层门禁（lint-imports）
```

后端监听 `127.0.0.1:8642`。本地 API 文档可在启动后访问 <http://127.0.0.1:8642/docs>。

生产前端默认向当前页面的同源 `/api` 发请求，适用于由 FastAPI 提供构建文件或反向代理统一入口的部署。若前端与 API 分域，在构建前设置 `VITE_API_BASE`；若后端修改了 `NARRIFY_CSRF_COOKIE`，同时设置 `VITE_CSRF_COOKIE_NAME`。这些 `VITE_` 值会写入前端构建结果，变更后需要重新构建；分域部署还需配置后端允许的来源和 Cookie 策略。

## 项目数据升级

升级到最新数据库 revision 时，迁移会为旧版 `/api/v1/projects` 创建的孤立 Project 补建同 ID 的 Workspace，也会为孤立 Workspace 补建同 ID 的 Project。迁移只修复数据库记录，不删除或移动用户工作空间文件。升级前仍应按部署流程备份数据库与工作空间。
