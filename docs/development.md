# 开发指南

[项目首页](../README.zh-CN.md) · [English](development.en.md) · [Windows 部署](windows.md) · [Linux 部署](../readme-linux.md) · [制作指南](production.md) · [运维指南](operations.md)

## 架构与源码入口

前端使用 Vue 3、TypeScript、Pinia 和 Vue Router。`src/api/` 是请求入口，`src/stores/` 管理状态，`src/views/` 是页面，`src/composables/` 组织工作台逻辑；通用组件和布局分别位于 `src/components/`、`src/layouts/`。

后端遵循 **api → services → platform → core** 单向依赖，由 `.importlinter` 检查。路由调用业务服务，平台层负责认证、数据库、任务、额度和存储，core 提供基础能力。`engines/`、API/Worker 入口尚未纳入该四层契约，不代表可以随意新增反向依赖。

`backend.main` 提供 API，`backend.worker_pool` 监督机械与模型 Worker，`backend.worker` 执行分工任务。PostgreSQL 保存用户、项目、任务、事件与 Outbox，Redis 协议服务负责队列，长时制作交给 Worker。TTS 使用 `tts-engine/` 中的独立子进程，共享 `.venv` 的依赖；FastAPI 进程不得导入 torch 或模型代码。

## 修改任务与工作台

`backend/platform/task_registry.py` 是任务类型的单一事实源。新增或修改任务时同步核对注册声明、提交校验、执行分发、计费与权限、GPU 分类，以及前端任务类型/标签。持续任务通过 `/api/v1/tasks*` 接口处理列表、历史、事件、取消和重试，不新增平行任务管理通路。

前端任务事件使用 `src/stores/task.ts` 的共享 SSE 通道。HTTP 统一经过 `src/api/client.ts`，沿用 Cookie session 和 CSRF。切换账号或项目时须使旧请求失效，避免旧结果写回；刷新任务状态也要处理断线与重新同步。资源扫描、打包和清理同样通过 Worker，不在 API 请求中递归扫描或同步执行长任务。

数据库模型变更添加 Alembic 迁移，保持 head 与模型一致。路径与子进程逻辑需检查 Windows/Linux 差异、文件身份、符号链接/junction、取消与结果发布边界；用户文件和运行配置不属于源码清理范围。

## 环境与常用命令

先按部署指南建立 `.venv` 和前端依赖。基础运行与开发检查不要求 GPU；完整 `backend/requirements.txt` 同时包含应用、音频、TTS 和测试依赖，直接安装会带入大型模型依赖。基础环境遵循部署指南的清单；TTS 安装与真实合成另按平台指南执行。

已有基础环境可补充开发测试工具，以及测试用到的音频库：

```powershell
.\.venv\Scripts\python.exe -m pip install 'pytest>=9.1' 'pytest-xdist>=3.8' 'httpx2>=2.13' import-linter 'soundfile>=0.12' 'pydub>=0.25' 'numpy>=2.0'
npm.cmd ci
npm.cmd run dev
.\.venv\Scripts\python.exe -m backend.main
.\.venv\Scripts\python.exe -m backend.worker_pool
```

`dev`、API 和 Worker 池是常驻进程，请在三个独立终端运行对应启动命令。每个后端终端都要先导入与 `.env` 一致的 `NARRIFY_*` 环境。Linux 使用 `.venv/bin/python` 与 `npm`；统一启动入口见部署指南。不要同时启动重复 API/Worker 实例而忽略连接与 GPU 预算。

```powershell
npm.cmd run typecheck
npm.cmd run build
npm.cmd run build:all
npm.cmd run lint:imports
.\.venv\Scripts\python.exe -m pytest backend/tests -n 4 --dist loadscope
```

`build` 包含 Vue/TypeScript 类型检查与 Vite 生产构建；`build:all` 再执行 backend/tts-engine 编译检查与分层门禁，不替代测试。输出 `dist/` 不手动编辑或提交。

生产前端默认向页面同源 `/api` 发请求，可由 FastAPI 提供 `dist/` 或通过反向代理统一入口。分域部署在构建前设置 `VITE_API_BASE`；后端修改 `NARRIFY_CSRF_COOKIE` 时对应设置 `VITE_CSRF_COOKIE_NAME`。值会写入构建产物，修改后必须重新构建；同时配置后端 `NARRIFY_CORS_ORIGINS` 与 Cookie/HTTPS 策略，参见部署及运维指南。

## 回归与 CI

本地后端完整测试固定用 **`-n 4 --dist loadscope`**。同文件的模块级数据库状态必须留在同一 worker；不要改成 `-n auto` 或省略 `loadscope`。测试会覆盖部署环境变量，使用临时 SQLite 和存储目录；默认无需本机 PostgreSQL/Memurai。真实数据库、Redis、FFmpeg 和平台条件按测试探测执行或跳过，阅读 skip 原因，不把跳过当成验收通过。真实 GPU 合成需要单独冒烟验证。

按修改范围选择前端回归：

| 修改范围 | npm 脚本 |
| --- | --- |
| Pinia 状态隔离 | `test:state-isolation` |
| 章节核对 / 文本解析 / 角色音色 | `test:workbench` / `test:script-parse-workbench` / `test:voices-workbench` |
| 制作工作台 / 总览 / 项目卡片 | `test:production-workbench` / `test:project-overview` / `test:project-cards` |
| 任务中心 / 我的资源 / 管理台 / GPU 设置 | `test:task-center` / `test:resources` / `test:admin-console` / `test:gpu-scheduler` |
| 页面布局 | `test:mobile-layout` / `test:ten-row-layout`，以及相应 `*-browser` 脚本 |

完整列表以 `package.json` 为准。浏览器回归的启动条件查看对应脚本，不能把它们等同于沙箱单元回归。涉及前端或共享流程时运行 typecheck 与 build，涉及后端依赖关系运行 lint:imports，后端修改按仓库指南运行完整套件。

当前 GitHub CI 的前端 job 使用 Node 22，运行生产构建及选定状态、总览、任务、制作、资源、管理和工作台回归；后端 job 使用 Python 3.14，安装 FFmpeg、排除模型依赖后执行编译、分层与完整测试。CI 为双核 runner 使用 **`-n 2 --dist loadscope -rs`**，与本地四 worker 规范分开。CI 不执行真实 GPU 模型验收。

## 提交与资料

Python 使用 4 空格，TypeScript/Vue 延续 2 空格；优先复用现有任务、文件和通用工具。提交前检查完整差异，仅包含相关源码/文档；采用 Conventional Commits，不提交凭据、虚拟环境、构建产物、日志、生成音频或工作空间。按当前用户指令执行提交与推送，不能因完成修复自行提交。

模型、第三方库和素材许可需分别核对；本项目许可证见 [LICENSE](../LICENSE)。生产运行、连接预算、GPU 与数据迁移见 [运维指南](operations.md)。
