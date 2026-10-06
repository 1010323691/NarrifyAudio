# 仓库指南

## 项目结构与模块组织

Narrify Audio 是 Windows 本地有声书制作工作台，由 Vue 3 + TypeScript 前端、FastAPI/Python API、独立 Worker、PostgreSQL 16 和 Memurai（Redis 协议兼容）组成。

- `src/`：前端代码；HTTP 客户端放在 `src/api/`，Pinia 状态放在 `src/stores/`，页面模块放在 `src/views/`，通用界面组件放在 `src/components/`，工作台组合逻辑放在 `src/composables/`，布局放在 `src/layouts/`。
- `backend/`：API 路由位于 `backend/api/`，业务编排位于 `backend/services/`，任务、认证、额度与存储等平台逻辑位于 `backend/platform/`，基础设施位于 `backend/core/`，业务引擎位于 `backend/engines/`。
- `backend/main.py` 与 `backend/worker_pool.py`：分别为 API 和后台任务池入口；任务池监督机械与模型 Worker，长时制作任务由 Worker 执行。
- `backend/migrations/`：Alembic 数据库迁移；pytest 测试位于 `backend/tests/`。
- `tts-engine/`：独立的 TTS 工作进程代码；大型模型依赖安装到共享 `.venv`，但仍通过子进程运行，不在 FastAPI 进程内导入。
- `backend/resources/`：提示词及其他运行时资源。`dist/` 是构建产物，不应手动编辑。
- `launch/`：Windows 与 Linux 应用、数据服务的启停入口，以及开发预览启动脚本；Linux 脚本使用 LF 换行，服务部署遵循 `readme-linux.md`。
- `scripts/`：构建、分层检查与前端回归脚本；`docs/`：项目和架构设计文档。

## 架构约束

- 后端遵循 `api → services → platform → core` 单向依赖，由 `.importlinter` 强制检查；不得引入反向导入。`engines/`、`main.py` 与 `worker.py` 暂未纳入该分层契约。
- 长时操作通过任务提交交给 Worker，不在 API 请求中同步执行。任务注册表以 `backend/platform/task_registry.py` 为单一事实源；修改任务类型时同步检查提交校验、执行分发和前端类型、标签。
- 任务列表、历史、事件、取消与重试统一使用 `/api/v1/tasks*`；前端任务事件通过 `src/stores/task.ts` 的 SSE 通道处理。
- 前端 HTTP 请求统一经 `src/api/client.ts`，沿用 Cookie session 与 CSRF 机制。切换账号或项目时，状态重置必须使在途请求失效，避免旧结果写回。
- TTS 保持子进程隔离；FastAPI 进程不得导入 torch 或模型代码。

## 构建、测试与开发命令

以下命令均在仓库根目录的 PowerShell 中执行：

```powershell
npm.cmd install
.\.venv\Scripts\python.exe -m pip install -r .\backend\requirements.txt
.\launch\start-data-services.ps1                    # 启动 PostgreSQL + Memurai Windows 服务
.\launch\start.ps1                                  # 检查数据服务、按需迁移并启动 API + Worker + Vite
npm.cmd run dev                              # 启动 Vite（127.0.0.1:5173）
.\.venv\Scripts\python.exe -m backend.main    # 启动 FastAPI（127.0.0.1:8642）
.\.venv\Scripts\python.exe -m backend.worker_pool  # 启动机械与模型 Worker 池
.\launch\stop-data-services.ps1                     # 先停止 API/Worker，再停止数据服务
npm.cmd run typecheck                        # 前端 TypeScript/Vue 类型检查
npm.cmd run build                            # 类型检查 + 前端生产构建
npm.cmd run build:all                        # 前端构建 + backend/tts-engine 编译检查 + 分层门禁
npm.cmd run lint:imports                     # 单独执行后端分层门禁
npm.cmd run test:state-isolation              # Pinia 状态隔离回归
npm.cmd run test:workbench                    # 章节核对工作台回归
npm.cmd run test:script-parse-workbench       # 文本解析工作台回归
.\.venv\Scripts\python.exe -m pytest backend/tests -n 4 --dist loadscope
```

首次启动前按 `README.md` 配置本机 `.env`、应用数据库用户与数据库。`launch/start.ps1` 将 `.env` 中的 `NARRIFY_*` 设置导入进程环境；单独启动 API、Worker 或执行迁移时，需先在当前环境中设置相应变量。

首次使用音频合成、合并或角色配音前运行 `.\install_tts_env.ps1`，该脚本会把体积较大的 TTS 依赖安装到共享 `.venv`；默认目标为 Python 3.14，若真实 TTS 冒烟测试失败可用 `-PythonVersion 3.10 -Recreate` 回退。FFmpeg 和 ffprobe 应可从 PATH 找到；检测到 winget 安装的 SoX_ng 时，启动脚本会创建本地 `sox.exe` 兼容副本。

## 编码风格与命名约定

Python 使用 4 个空格缩进，TypeScript/Vue 延续现有的 2 个空格缩进。Vue 组件使用 `PascalCase`，前端函数和变量使用 `camelCase`，Python 函数、模块和测试名称使用 `snake_case`。优先复用已有的文件、任务管理和通用工具函数。不要提交构建产物、工作空间输出、凭据或本地虚拟环境。

## 测试指南

后端使用 `pytest`，测试文件命名为 `test_<功能>.py`，测试函数命名为 `test_<行为>`。修改功能时，应在对应测试文件中补充回归测试，并覆盖边界条件；涉及并发时也要覆盖并发场景。提交后端改动前运行完整测试套件；涉及前端或共享流程的改动还应运行 `npm.cmd run typecheck` 和 `npm.cmd run build`。

完整后端套件固定使用 `-n 4 --dist loadscope`，不要改用 `-n auto` 或省略 `loadscope`：部分平台测试依赖同文件内的模块级数据库状态，必须留在同一 worker 中执行。默认测试使用 SQLite 和临时存储，无需启动本机 PostgreSQL/Memurai；真实数据库或队列验收用例依照各自的环境条件执行或跳过。

修改 Pinia store、章节核对或文本解析工作台时，分别运行对应的 `test:state-isolation`、`test:workbench` 或 `test:script-parse-workbench` 回归脚本。修改后端依赖关系时运行 `npm.cmd run lint:imports`；`build:all` 包含编译和分层检查，但不替代测试套件。

## 提交与合并请求规范

提交信息使用 Conventional Commits 格式（如 `feat: ...`、`fix: ...`、`docs: ...`），描述可观察到的改动，正文可用中文；每个提交保持聚焦。提交前检查 `git status` 和完整差异，仅暂存本次相关文件；未明确要求时不执行 `git push`。合并请求应说明用户可见的行为变化，列出执行过的验证命令及结果，关联相关 issue 或需求；涉及界面改动时附上截图或录屏，并明确说明配置、工作空间产物或迁移方面的影响。

## 安全与配置提示

不得提交 API 密钥、本地工作空间路径、生成的音频、日志或虚拟环境。`.env`、`storage/`、`config/`、`logs/`、`.narrify/`、`music_library/`、`.backups/` 和工作空间输出均为本机运行时或用户数据，不作为源码清理，也不提交。

根目录 `setting.json` 作为默认配置模板纳入 Git；提交时保持工作空间路径为空，LLM key 仅使用 `local` 占位值，不提交运行时写入的本机路径或凭据。工作空间内的 `config/setting.json` 仍为用户数据。

数据库、队列和初始管理员配置放在本机 `.env`；LLM 凭据和制作参数通过应用设置管理，不写入代码、文档或前端。前端分域部署所需的 `VITE_API_BASE` / `VITE_CSRF_COOKIE_NAME` 在构建时生效，修改后需重新构建。

数据库模型变更应添加 Alembic 迁移，保持迁移 head 与模型一致；不可逆数据库变更前先备份。修改路径处理或子进程逻辑时要特别谨慎，尤其是涉及 ffmpeg、TTS 工作进程和用户文件操作的代码。
