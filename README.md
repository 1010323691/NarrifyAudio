# NarrifyAudio

一个 Web 应用,在一个地方完成中文有声书的完整制作流程:**排版与分册(排版 → 自动章节分析 → 每章一册)→ 文本解析(内含断句失败校验 / 纯归属标签删除 / 归属抽样)→ 角色配音 → 音频合成 → 音频合并 → 音频分集**,外加开始(工作空间管理)与设置。

当前仓库以 AudiobookStudio 的真实业务引擎为兼容基线，并逐步迁移到 NarrifyAudio 的多用户服务架构。新平台 API 位于 `/api/v1`，认证使用 HttpOnly Session Cookie + CSRF Token；用户、项目、文件、任务、额度预留和 Outbox 事件使用持久化数据库记录。

## 多用户平台开发配置

本地开发默认使用项目目录下的 SQLite 文件，仅用于方便启动；部署时必须设置 PostgreSQL 连接串，并关闭自动建表，使用 Alembic 迁移：

```powershell
$env:NARRIFY_DATABASE_URL = "postgresql+psycopg://narrify:change-me@localhost:5432/narrify"
$env:NARRIFY_AUTO_CREATE_SCHEMA = "false"
$env:NARRIFY_STORAGE_ROOT = "D:\\narrify-storage"
$env:NARRIFY_BOOTSTRAP_ADMIN_EMAIL = "admin@example.com"
$env:NARRIFY_BOOTSTRAP_ADMIN_PASSWORD = "replace-with-a-long-secret"
```

生产环境还应设置 `NARRIFY_COOKIE_SECURE=true`，并通过反向代理提供 HTTPS。注册、登录、项目和上传接口位于 `/api/auth` 与 `/api/v1/projects`；持久化任务提交位于 `/api/v1/tasks`，提交会在同一数据库事务内写入任务、额度预留、流水和 Outbox 事件。

所有平台工作空间都由服务端统一管理。管理员通过 `/api/v1/admin/settings/storage` 设置存储根目录；用户的目录固定落在 `<root>/<username>/<workspace-or-project-id>/` 下，客户端不能提交物理路径。项目文件、显式工作空间和后续产物均应复用这一规则；删除工作空间只做软删除并保留目录，物理清理由后续保留策略任务执行。

## 功能

- **开始** — 设置 / 清除工作空间;未设置工作空间时流水线锁定(后端写端点返回 409,入口禁用)
- **排版与分册** — 单一流程:对原始 TXT 做确定性排版(空白 / 段落 / 标点 / 章节,10 个可配置开关,结果内容保持、可重复排版)→ 自动分析章节 → **每章一个文件**分册(只在章节边界切割、绝不重编号,拼回所有分册可精确复现原文);章节号缺失/重号/乱序给出**非阻断**警告(逐条列出具体缺哪些章号 + 系统识别的章节格式),可选「不处理」继续或「重新上传原文」重跑;零章节时可选按整本继续(单个《全书》文件)
- **文本解析** — 「LLM → JSON」管线:从 `02_split_text/` 勾选一个或多个文本文件(可多选),每个文件作为独立任务并发调用 LLM(并发数可配),逐段生成逐行标注脚本 `03_parsed_json/<文件基名>.json`(角色 / 类型 / 演绎指令 / 停顿);解析任务内含三个检查阶段——断句失败校验(疑似断句失败的条目逐条重判)/ 纯归属标签删除(独立短标签条确定性删除、不经 LLM)/ 归属抽样(按比例抽条目重判 speaker,整书错误率读数记入日志与历史文件)——各阶段均可在「设置」页单独开关(默认全开);每文件独立成败、互不影响
- **角色配音** — 选择一个解析 JSON(或选「全部文件」一次性合并整本书所有角色),为角色生成声音画像(`voice_config.json` + 试听样本);阶段 2 可为每角色生成多条候选克隆音频(备选音频数:自动 / 2 / 4 / 6 / 8,候选间经随机种子产生差异),试听后单选其一为最终音色(未选择时默认使用第 1 条);支持一键全部、仅处理新增、或单角色按提示词重生成
- **音频合成** — 勾选一个或多个解析 JSON(支持全选 = 全部未合成文件,已完成文件不会被自动勾中)后批量合成:单个长时任务内按顺序逐文件合成(每个未完成文件启动一次共享 `.venv` 子进程、模型只加载一次;已全部完成的文件自动跳过、不加载模型);单行 / 单文件失败只记录、不打断其余文件;每行实时显示 已合成 / 总段落 · 角色 · 已就绪声音,文件全部完成时显示【已合成】;每个 JSON 合成成一个「音频包」(子文件夹 `05_audio_chunk/<JSON 基名>/`,含逐行音频与 `manifest.json`)
- **音频合并** — 从「音频包」列表(05_audio_chunk/ 的各子文件夹)选中一个包,按该包清单把逐行音频无损合并为 `06_audio_merge/<包名>.mp3`,支持换人 / 同人停顿
- **音频分集** — 选择一个已合并的有声书(06_audio_merge/),无损切分为若干集(`-c copy` 不重编码),输出到 `07_output/<源名>/`;支持按停顿位置智能对齐边界
- **长时任务系统** — 进度 / 日志 / 暂停 / 恢复 / 取消 / 重试;各流程页内联展示所启动任务的实时状态;失败任务自动隔离,不影响整个应用
- **设置** — 工作空间、各模块参数(排版开关 / 分集 / TTS 模型与停顿)、ffmpeg 路径、界面主题、日志级别,持久化保存、重启自动恢复

## 技术栈

- Frontend: Vue 3 + TypeScript + Vite + Tailwind CSS + Pinia + vue-router(瘦客户端,只渲染 UI)
- Backend: Python FastAPI + Uvicorn + Pydantic(监听 `127.0.0.1:8642`,**所有处理逻辑都在这里**)
- TTS: 本地 Qwen3-TTS — 与后端共用 `.venv`(默认 Python 3.14 + torch (CUDA) + qwen-tts),通过一次性子进程编排;模型仍与 FastAPI 进程隔离

## 环境要求

- Windows 10/11(也支持 macOS / Linux)
- Python 3.10+(本仓库 `.venv` 为 3.14)
- Node.js 18+
- ffmpeg / ffprobe(「音频分集」与「音频合并」需要;在 `PATH` 中,或在设置里填绝对路径)
- TTS 相关功能(可选):共享 `.venv` 中的 Python 3.14 + torch (CUDA) + qwen-tts,数 GB 依赖由 `install_tts_env.ps1` 安装;若 3.14 的真实 TTS 冒烟测试失败,可用脚本的 3.10 回退参数重建

## 安装

### 1. 克隆项目

```powershell
git clone <仓库地址> audiobookstudio
cd audiobookstudio
```

### 2. 安装依赖

```powershell
# Python 后端 + TTS(共享 .venv,默认 Python 3.14)
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\backend\requirements.txt

# 前端
npm.cmd install

# 准备共享 TTS 环境(可选,首次下载数 GB 的 torch)
powershell -ExecutionPolicy Bypass -File install_tts_env.ps1

# 如果 3.14 的真实 TTS 冒烟测试失败,显式回退到 Python 3.10
powershell -ExecutionPolicy Bypass -File install_tts_env.ps1 -PythonVersion 3.10 -Recreate
```

### 3. 启动

Windows 推荐直接双击 `start.bat`，脚本会检查共享 `.venv` 和前端依赖，启动后端与 Vite，并在服务就绪后打开浏览器。

也可以在 PowerShell 中运行：

```powershell
.\start.ps1
```

手动启动方式如下：

```powershell
.venv\Scripts\python -m backend.main    # → http://127.0.0.1:8642
```

再任选一种方式打开界面:

| 方式 | 命令 | 打开地址 |
| --- | --- | --- |
| 浏览器开发态 | `npm run dev` | `http://localhost:5173` |
| 浏览器 + 后端托管(最简) | 先 `npm run build`,再开后端 | `http://127.0.0.1:8642` |

macOS / Linux 将 `.venv\Scripts\python` 换为 `.venv/bin/python` 即可。

## 构建

前后端统一构建命令：

```powershell
npm.cmd run build:all
```

该命令依次执行前端 `vue-tsc + vite build`，以及后端 `backend/`、`tts-engine/` 的 Python 语法与字节码编译检查；不会下载模型，也不会运行完整测试套件。

## 使用方法

1. **开始**:选择一个文件夹作为工作空间(未设置时流水线锁定)→ 工程配置、日志与所有产物都落在该工作空间的固定子目录(`config/` · `logs/` · `00_temp` … `07_output`)
2. **排版与分册**:选择 TXT 文件 → 勾选排版选项(句断、对话分行、章节检测、标点规范等)→ 点「开始排版」(自动触发章节分析,预览章节表与各分册文件名)→ 点「开始分册」(可选同时打包 zip)→ 每章生成一个分册文件;缺章号时出现非阻断警告,可「不处理,继续」或「重新上传原文」;完成后一键「前往下一步(文本解析)」
3. **文本解析**:在 `02_split_text/` 勾选要解析的文本文件(可多选)→ 配置 LLM(base_url / api_key / 模型)、生成参数(含并发数)与 Prompt → 点「开始解析」,每个文件作为独立任务并发逐段调用 LLM,分别生成 `03_parsed_json/<文件基名>.json`;每文件独立状态(待处理 / 解析中 / 已完成 / 失败),一个失败不影响其他
4. **角色配音**:选择一个解析 JSON(03_parsed_json/,或选「全部文件」合并整本书所有角色)→ 一键为角色生成声音画像(voice_config.json + 试听样本),也可仅处理新增角色或单角色重生成;阶段 2 可选备选音频数(自动 / 2 / 4 / 6 / 8),生成后在角色列表逐个试听候选并单选最终音色(未选择时默认使用第 1 条)
5. **音频合成**:勾选一个或多个解析 JSON(03_parsed_json/,可多选;「全选未合成文件」不含已完成文件)→ 启动批量合成(长时任务,进度 / 日志内联显示在页面;行内 已合成 / 总段落 实时爬升,完成即现【已合成】)→ 在 `05_audio_chunk/` 产出逐行音频与 `manifest.json`
6. **音频合并**:按清单把逐行音频合并为 `06_audio_merge/cloned_audiobook.mp3`(换人 / 同人停顿可配)
7. **音频分集**:选择音频文件(通常是合并成品)→ 设置目标单集时长(默认 10:00)与命名格式 → 启动「停顿检测 + 切割」(长时任务,进度 / 日志内联显示在页面)→ 完成后可下载 zip 或输出到源音频同级「分集」文件夹
8. **设置**:配置工作空间、各模块参数(排版开关 / 分集 / TTS 模型与停顿)、ffmpeg / ffprobe 路径、界面主题(跟随系统 / 亮 / 暗)、日志级别

所有产物按模块落在工作空间的固定子目录下:`01_input/`(排版文本)· `02_split_text/`(分册)· `03_parsed_json/`(解析 JSON,`<基名>.json` = 唯一产物;旧工程遗留的 `<基名>_checked.json` 为惰性文件,不再读取/生成)· `04_voice_profiles/`(角色声音)· `05_audio_chunk/`(合成片段)· `06_audio_merge/`(合并成品)· `07_output/`(最终分集);产物通过页面上的下载链接获取。

## 项目结构

```text
项目目录/
├── backend/                 # Python 后端(所有处理逻辑)
│   ├── main.py              # 入口:路由、CORS、/api/health、静态托管 dist/
│   ├── api/                 # 瘦路由:text / book / audio / tts / script + workspace / tasks / config / files
│   ├── engines/             # 核心算法:排版 / 分册(每章一册)/ 解析(含解析内检查:断句校验 / 标签删除 / 归属抽样)/ 音频切割 / TTS(批量合成 / 合并 / 角色配音)
│   ├── core/                # 目录布局、持久化配置、任务系统、日志
│   ├── tests/               # pytest 测试套件(546 个测试)
│   └── requirements.txt     # 精简依赖:fastapi / uvicorn / pydantic / python-multipart
├── src/                     # Vue 3 前端(瘦客户端)
│   ├── api/                 # HTTP 客户端(固定指向 127.0.0.1:8642)
│   ├── views/               # 八个模块:开始 / 排版与分册 / 文本解析 / 角色配音 / 音频合成 / 音频合并 / 音频分集 / 设置
│   ├── stores/              # Pinia 状态
│   ├── components/          # 布局 + 原子组件
│   └── utils/               # 文件选择 / 下载(浏览器 input + 上传)
├── tts-engine/              # TTS 工作进程(运行在共享 .venv,一次性子进程)
├── install_tts_env.ps1      # 准备共享 .venv(Python 3.14 + torch,数 GB)
├── app.json                 # 运行时配置(gitignore):通用配置模板 + 当前工作空间指针;工程配置 / 日志 / 产物都在用户工作空间内
├── dist/                    # 前端构建产物(gitignore),后端可直接托管
├── package.json
├── vite.config.ts
└── README.md
```
