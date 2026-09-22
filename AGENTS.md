# 仓库指南

## 项目结构与模块组织

AudiobookStudio 是 Vue 3 + TypeScript 前端与 FastAPI/Python 后端组成的应用。

- `src/`：前端代码；HTTP 客户端放在 `src/api/`，Pinia 状态放在 `src/stores/`，页面模块放在 `src/views/`，通用界面组件放在 `src/components/`。
- `backend/`：API 路由位于 `backend/api/`，基础设施位于 `backend/core/`，处理逻辑位于 `backend/engines/`，pytest 测试位于 `backend/tests/`。
- `tts-engine/`：独立的 TTS 工作进程代码；大型模型依赖安装到共享 `.venv`，但仍通过子进程运行，不在 FastAPI 进程内导入。
- `backend/resources/`：提示词及其他运行时资源。`dist/` 是构建产物，不应手动编辑。

## 构建、测试与开发命令

以下命令均在仓库根目录的 PowerShell 中执行：

```powershell
npm.cmd install
\.venv\Scripts\python.exe -m pip install -r .\backend\requirements.txt
npm.cmd run dev                              # 启动 Vite 前端
\.venv\Scripts\python.exe -m backend.main # 启动 FastAPI（127.0.0.1:8642）
.\start.ps1                                 # 检查环境并启动后端 + 前端
npm.cmd run build                            # 类型检查并构建生产版本
npm.cmd run build:all                        # 前端构建 + 后端 Python 编译检查
npm.cmd run typecheck                        # 执行前端 TypeScript/Vue 类型检查
\.venv\Scripts\python.exe -m pytest backend/tests
```

首次使用音频合成、合并或角色配音前运行 `install_tts_env.ps1`，该脚本会把体积较大的 TTS 依赖安装到共享 `.venv`；默认目标为 Python 3.14，若真实 TTS 冒烟测试失败可用 `-PythonVersion 3.10 -Recreate` 回退。

## 编码风格与命名约定

Python 使用 4 个空格缩进，TypeScript/Vue 延续现有的 2 个空格缩进。Vue 组件使用 `PascalCase`，前端函数和变量使用 `camelCase`，Python 函数、模块和测试名称使用 `snake_case`。优先复用已有的文件、任务管理和通用工具函数。不要提交构建产物、工作空间输出、凭据或本地虚拟环境。

## 测试指南

后端使用 `pytest`，测试文件命名为 `test_<功能>.py`，测试函数命名为 `test_<行为>`。修改功能时，应在对应测试文件中补充回归测试，并覆盖边界条件；涉及并发时也要覆盖并发场景。提交后端改动前运行完整测试套件；涉及前端或共享流程的改动还应运行 `npm.cmd run typecheck` 和 `npm.cmd run build`。

## 提交与合并请求规范

近期提交信息简洁、以功能为中心，常使用中文，有时会附带简短原因说明。请描述可观察到的改动，并保持每个提交聚焦。合并请求应说明用户可见的行为变化，列出执行过的验证命令及结果，关联相关 issue 或需求；涉及界面改动时附上截图或录屏，并明确说明配置、工作空间产物或迁移方面的影响。

## 安全与配置提示

不得提交 API 密钥、本地工作空间路径、生成的音频、日志或虚拟环境。应将用户工作空间中的 `config/`、`logs/` 和输出目录视为用户数据。修改路径处理或子进程逻辑时要特别谨慎，尤其是涉及 ffmpeg 和 TTS 工作进程的代码。
