# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

NarrifyAudio 是 Windows 本地有声书制作工作台：Vue 3 + TypeScript 前端、FastAPI API、独立 Worker 进程、PostgreSQL 16、Memurai（Redis 协议兼容）。第一阶段代码治理已实施完毕（治理计划文档 `docs/plan.md` 已由负责人删除，结论与约束已沉淀进本文件和代码注释，历史细节可查 git log）。

业务流水线（长时执行全部在 Worker 进程，API 层没有进程内业务线程）：
文稿上传 → 文本排版/分册（`text.format` / `book.split`）→ LLM 解析（`script.parse`）→ 角色音色（`voices.*`）→ 批量合成（`tts.batch`，TTS 子进程）→ 合并（`tts.merge`）→ 分集（`audio.silences` / `audio.cut`）→ BGM 分析与混音（`bgm.*`）→ 打包导出（`audio.zip` / `audio.export`）。

## 常用命令

均在仓库根目录 PowerShell 执行：

```powershell
npm.cmd install
.\.venv\Scripts\python.exe -m pip install -r .\backend\requirements.txt   # 后端与 TTS 共用 .venv
.\install_tts_env.ps1 -PythonVersion 3.14   # TTS 大依赖；真实 TTS 冒烟不兼容时改 -PythonVersion 3.10 -Recreate
```

```powershell
npm.cmd run dev                                  # Vite 前端（5173，/api 代理到 8642）
.\.venv\Scripts\python.exe -m backend.main       # FastAPI（127.0.0.1:8642，同时托管 dist/ 静态产物）
.\.venv\Scripts\python.exe -m backend.worker     # Worker（outbox → Redis Streams）
.\start-data-services.ps1                         # 启动 PostgreSQL + Memurai Windows 服务
.\start.ps1                                       # 全栈：检查数据服务 → 跑迁移 → 启动 API/Worker/Vite
.\stop-data-services.ps1                          # 先停 API/Worker 再停数据服务
```

```powershell
npm.cmd run typecheck            # vue-tsc 前端类型检查
npm.cmd run build                # 类型检查 + 生产构建（dist/）
npm.cmd run build:all            # 前端构建 + 后端 compileall + 分层门禁（lint-imports）
npm.cmd run lint:imports         # 分层门禁单独运行
npm.cmd run test:state-isolation # 前端状态隔离回归（node:test 沙箱跑 Pinia store）
.\.venv\Scripts\python.exe -m pytest backend/tests -n 4 --dist loadscope  # 后端全量测试（4 进程并行，见下方说明）
.\.venv\Scripts\python.exe -m pytest backend/tests/test_script.py  # 单个文件
.\.venv\Scripts\python.exe -m pytest backend/tests/test_script.py -k 名称片段  # 单个用例
```

测试默认跑在 sqlite + 临时存储上（`backend/tests/conftest.py` 覆盖 `NARRIFY_DATABASE_URL` / `NARRIFY_STORAGE_ROOT`），全套件不需要本机 PostgreSQL/Memurai 在运行；少数真实 DB 用例（`test_migrations`、`test_postgres_cancellation_concurrency` 等）带 `skipif`，无 DB 时自动跳过。全量套件用 pytest-xdist 并行跑，worker 数**固定 4**（实测：串行 90s，`-n 2/4/8/12/16` = 61s/37s/61s/57s/71s——套件含大量派生子进程的测试，超过 4 worker 后 CPU 超额订阅，比串行还慢，故不用 `-n auto`）；`loadscope` 不可省——`test_platform` 有用例依赖同文件前序用例留下的模块级 DB 状态，同一文件必须整体留在一个 worker 内按序执行。

提交后端改动前跑完整后端测试套件；涉及前端或共享流程时额外跑 `npm.cmd run typecheck` 和 `npm.cmd run build`。不可逆数据库变更前先备份（历史备份在 `.backups/`）。

## 后端分层契约（强制）

`api → services → platform → core` 单向依赖，由 import-linter 按 `.importlinter` 强制执行，`npm run build:all` 包含该门禁，反向导入直接构建失败（当前无豁免边）。`engines/`、`main.py`、`worker.py` 暂未纳入分层。

- `backend/api/` — FastAPI 路由。`main.py` 中 PLATFORM_ROUTERS 直接注册；7 个 legacy 路由（config/files/audio/bgm/tts/script/music）统一挂 `require_legacy_access` 依赖（认证 + 状态变更请求要求 CSRF）。
- `backend/services/` — 业务编排（项目文件系统、任务视图等）。
- `backend/platform/` — 多用户平台核心：任务注册表、任务生命周期、事务 Outbox → Redis Streams、额度账本、session/安全、存储与迁移。
- `backend/core/` — 配置、路径布局、并发门、`pathio`、`file_lock`、可观测性等基础件。
- `backend/engines/` — 业务引擎（script/tts/merge/bgm/book/text/audio/music/voices）。

## 任务系统

- 18 种任务类型（6 平台直连 + 12 legacy 引擎）的单一事实源是 `backend/platform/task_registry.py`（`task_types.py` 只做 re-export）。
- 提交链路：路由 → `platform/task_submission.py:submit_task_record` → 事务内写 Task + OutboxEvent → Worker 的 `outbox.publish_pending` 投递 Redis Streams（租约 + XAUTOCLAIM 恢复，`recover_database_tasks` 兜底数据库侧遗留任务）→ 两个执行分发器：`platform/task_worker.py:execute_claim`（新类型）与 `platform/engine_task_executor.py:execute_engine_task`（legacy 引擎类型）。
- 任务 HTTP 表面已统一为 `/api/v1/tasks*` 单套（列表/历史/SSE `GET /api/v1/tasks/stream`/取消/重试/批量控制都在 `api/platform_tasks.py` 一个 router；前端 `src/api/tasks.ts` + `durableTasks.ts` 共享它，`stores/task.ts` 以单条多路复用 SSE 承载全应用任务事件）。旧的「legacy SSE 表面 vs v1 轮询」双表面描述已作废，不存在双任务数据源。
- 额度按操作计费（`platform/quota.py` 的 `QuotaHold`）是现行生效路径；任务级 `QuotaReservation` 已退役。
- 工作区绑定：请求级 ContextVar（`main.py` 的 `bind_authenticated_workspace` 中间件，Starlette 可能在不同线程跑同步依赖，故不能在 auth 依赖里绑）+ 任务级配置快照（`core.config`，任务启动时绑定）。

## 需求落点速查（改一个功能，从哪进）

- **改某个流水线任务的行为**（合成/合并/BGM/分集…）：以任务类型字符串（`tts.merge`、`bgm.mix`…）为锚点，`platform/task_registry.py` 的表一行给出计费/权限/executor 绑定；executor 实现按 `legacy_engine` 分两处：`task_worker.DIRECT_EXECUTORS`（6 个新类型）或 `engine_task_executor.ENGINE_BRANCHES`（12 个 legacy 类型），实际算法在 `engines/*`。注意：分发目前处于"影子双跑"过渡期（注册表裁决 vs 旧 if-chain 交叉核对，不一致 fail closed），改分发逻辑两侧要同改。
- **新增任务类型**：`task_registry` 表 + 对应分发器的 executor + `platform/task_submission.py` 提交校验 + 前端 `src/utils/taskTypes.ts` / `taskLabels.ts`（类型前缀知识还重复在 `ProjectOverview.vue`、`Admin.vue`，需同步）。
- **UI 页面/交互**：`src/views/X.vue` + `src/router.ts` 注册（项目阶段页面加 `meta: { projectStage: true }`）+ `src/api/` 对应客户端；长时操作一律走任务提交 + 轮询/SSE，不在请求里同步跑。
- **配置项**：项目配置随工程存 DB（`/api/config` 读写活动工作区配置，永不写根模板）；LLM 凭据等由管理控制台管（`api/admin.py` + `platform/system_config.py` 的 `SystemConfig`）；`core/config.py` 的功能默认值由 `platform.system_config` 注册的 provider 供给（分层契约要求 core 不反向 import platform）。
- **额度/计费**：`platform/quota.py`（按操作 `QuotaHold`）。
- **DB 结构变更**：`backend/migrations/` 加 Alembic 迁移（链从 0001 起，head 须与 `platform/models.py` 一致；不可逆变更先备份）。
- **并发/并行度**：`core/concurrency.py` 的 resize 表面（`set_concurrency` / `set_merge_concurrency`）已删（0 生产调用），`gate()` / `merge_gate()` 恒 limit=1——LLM/BGM/合并分析实际串行。真正确定并行度的是引擎侧的 acquire 点（`engines/bgm.py`、`merge.py`、`music.py`、`script.py`），改并行度从那里入手，concurrency 文件里已没有可调的旋钮。
- **工作区路径/文件产物**：布局在 `core/paths.py`，存储对象与安全文件名在 `platform/storage.py`（`safe_display_name` 规则与 `tts_manifest` 内联副本**不等价**——截断与兜底行为不同，合并会改变现网路径，不要顺手统一）。

## TTS 子进程隔离

TTS 引擎（`tts-engine/tts_worker.py`，约 2500 行）是 one-shot 子进程，FastAPI 进程永不 import torch/模型代码。通信为 CLI 参数 + 磁盘 JSON + stdout 行协议；退出码 124 = 看门狗超时（`engines/tts.py:run_tts_subprocess` 将其映射为可降批重试信号，而不是整任务失败）。`AUDIOTTS_WORKER` 环境变量可覆盖 worker 脚本路径。FFmpeg 需在 PATH。

## 前端架构

- 哈希路由，两个互不链接的门面：用户工作台（`/`，`MainLayout`，`requiresUser`）与管理控制台（`/admin`，`AdminLayout`，`requiresAdmin`），各有独立登录页与角色守卫（`src/router.ts`）。`meta.projectStage` 视图在无活跃项目时重定向到 dashboard。
- `src/api/client.ts` 是唯一 HTTP 入口：cookie session + 从 `narrify_csrf` cookie 取 `X-CSRF-Token`。开发走 Vite `/api` 代理；生产默认同源 `/api`（FastAPI 托管 `dist/`，GET catch-all 回 SPA shell，未知 `/api/*` 保持诚实 404）。分域部署在**构建时**设 `VITE_API_BASE` / `VITE_CSRF_COOKIE_NAME`，改完需重新构建。
- Pinia stores（`src/stores/`）带竞态保护：reset 必须使在途请求失效（旧账号的配置/创建结果不得复活）。这些场景由 `scripts/test-state-isolation.mjs`（`npm run test:state-isolation`，node:test + vm 沙箱加载真实 store 代码）钉住——改 store 行为时该文件是回归门禁。
- `dist/` 是构建产物，不手改。

## 运行时数据与配置

- `storage/`、`config/`、`logs/`、`.narrify/`、`music_library/`、`app.json`（根，工作空间指针 + 模板）、`.backups/` 均为本机运行时/用户数据，已 gitignore——不当源码清理，也不提交。`music_library/` 是跨工程共享的全局音乐库。
- `.env`（gitignored）：`NARRIFY_DATABASE_URL`、`NARRIFY_REDIS_URL`、bootstrap 管理员账号。`postgres` 管理员密码仅供人工 psql 运维，不入 `.env`。LLM 凭据与制作参数在应用内设置页配置，不写进代码/文档/前端。

## 约定

- Python 4 空格缩进、`snake_case`；TS/Vue 2 空格、组件 `PascalCase`、函数变量 `camelCase`。优先复用已有的文件、任务管理、通用工具函数。
- 提交信息简洁、描述可观察到的改动、一次提交聚焦一个变更（仓库历史中英文皆有）。
- 涉及路径处理与子进程逻辑（ffmpeg、TTS worker）要特别谨慎；不提交 API 密钥、本机工作空间路径、生成的音频、日志或虚拟环境。
