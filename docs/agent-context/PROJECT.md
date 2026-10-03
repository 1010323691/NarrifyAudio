# 项目概览
协议：context-protocol/v2
更新时间：2026-10-01T19:45+09:00
观察来源与范围：仓库根 `CLAUDE.md`（主设计书，本次未逐行复核）、git log/status、PR #33 记录；代码级核实仅限角色配音工作台与章节列表（见 session S-20261001T104021Z-k3d8f2）
探索覆盖：partial

## 目标与主要使用者

Windows 本地有声书制作工作台：文稿上传 → 排版分册 → LLM 解析 → 角色配音 → 批量 TTS → 合并/分集 → BGM 混音 → 打包导出。使用者为本地制作者；管理控制台（`/admin`）管 LLM 凭据与系统配置。详见 `CLAUDE.md` 项目概述。

## 一级功能领域

| 领域 | 用户能做什么 | 主要源码入口 | 探索覆盖 |
|---|---|---|---|
| 文本排版/分册 | 上传排版、章节核对 | `src/views/TextFormat.vue`、`composables/useTextFormatWorkbench.ts` | partial（本次改过列表） |
| 文本解析 | LLM 解析工作台、行级状态 | `src/views/ScriptParse.vue`、`composables/useScriptParseWorkbench.ts` | partial（本次改过列表） |
| 角色配音 | 音色选择/克隆/合并、两阶段制作面板 | `src/views/Voices.vue`、`views/voices/VoicesWorkbench.vue` | known（本次深读） |
| 批量合成/音频 | 批量 TTS、合并、分集 | `src/views/BatchTTS.vue`、`backend/engines/tts.py|merge.py` | partial（本次只读 BatchTTS） |
| BGM/打包 | 分析混音、导出 | `backend/engines/bgm.py`、`api/audio*` | unknown |
| 平台任务/额度 | 任务提交、SSE、取消重试、额度 | `backend/platform/task_registry.py`、`task_submission.py`、`quota.py` | unknown（依 CLAUDE.md 描述） |
| 管理控制台 | 用户/LLM 凭据/系统配置 | `src/views/Admin.vue`、`api/admin.py` | unknown |

## 技术栈与运行边界

| 组件 | 技术与边界 |
|---|---|
| 前端 | Vue 3 + TypeScript + Vite（5173 开发，`/api` 代理 8642）；哈希路由，用户/管理双门面 |
| API | FastAPI（127.0.0.1:8642），生产托管 `dist/` 静态产物 |
| Worker | 独立进程，outbox → Redis Streams（Memurai）→ 两个分发器（新类型/legacy 引擎） |
| 数据 | PostgreSQL 16（Alembic 迁移链）；运行时数据在 gitignored 目录（storage/ 等） |
| TTS | one-shot 子进程（tts-engine/tts_worker.py），CLI+磁盘 JSON+stdout 协议；FastAPI 永不 import torch |
| 门禁 | import-linter 分层 `api→services→platform→core`；前端沙箱回归脚本（node:test + vm）钉住三个工作台 composable |

## 典型数据与调用流

1. 任务提交：路由 → `submit_task_record`（事务写 Task+OutboxEvent）→ Worker `outbox.publish_pending` → Redis Streams → `execute_claim` / `execute_engine_task`。
2. 长时页面交互（如角色配音）：请求级 ContextVar 绑工作区 + 任务级配置快照；前端走任务提交 + SSE/轮询，不在请求里同步跑。

## 全局开发约束

- 分层单向依赖由 import-linter 强制，反向导入构建失败（当前无豁免边）；`engines/`、`main.py`、`worker.py` 暂未纳入。
- 提交后端改动前跑完整 pytest（`-n 4 --dist loadscope`，worker 数固定 4，勿改 auto）；涉及前端额外 `typecheck` + `build`。
- PR 创建/评论/合并一律走 `gh`（仓库已私有，2026-09-27 起）；合并用 `gh pr merge`（git credential 旧 token 无 merge 权限）。
- 前端三个工作台 composable 各有沙箱回归门禁（`test:workbench` / `test:script-parse-workbench` / `test:voices-workbench`），改 store 行为另有 `test:state-isolation`。

## 现有资料与知识缺口

主设计书 = 仓库根 `CLAUDE.md`（含常用命令、需求落点速查）；治理计划文档已按负责人意图删除，勿恢复。缺口：后端/platform/engines 领域无本目录模块卡片；CI 为 GitHub Actions 双 job（前端/后端），合并前须全绿。
