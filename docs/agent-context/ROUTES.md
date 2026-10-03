# 领域路由
协议：context-protocol/v2
更新时间：2026-10-01T19:45+09:00

## 一级领域

| 领域 | 功能/检索关键词 | 主要源码路径 | 模块/二级索引入口 | 任务/命令/坑入口 |
|---|---|---|---|---|
| 角色配音 | voices、音色、克隆、合并角色、overlay、Tab 焦点 | `src/views/Voices.vue`、`src/views/voices/` | [modules/voices/overlay-focus.md](modules/voices/overlay-focus.md) | not_applicable（无专用命令/坑条目） |
| 文本排版/分册 | TextFormat、章节核对、useTextFormatWorkbench | `src/views/TextFormat.vue`、`views/textformat/` | not_applicable（行为见 CLAUDE.md + 回归脚本 `scripts/test-textformat-workbench.mjs`） | not_applicable |
| 文本解析 | ScriptParse、行级状态、useScriptParseWorkbench | `src/views/ScriptParse.vue`、`views/scriptparse/` | not_applicable（回归脚本 `scripts/test-script-parse-workbench.mjs`） | not_applicable |
| 批量合成/音频 | BatchTTS、tts.batch、activeScript | `src/views/BatchTTS.vue`、`backend/engines/tts.py` | not_applicable | not_applicable |
| 平台任务/额度 | task_registry、outbox、QuotaHold | `backend/platform/` | unknown（待探索，先读 CLAUDE.md「任务系统」） | not_applicable |
| BGM/打包 | bgm.mix、audio.zip | `backend/engines/bgm.py` | unknown | not_applicable |
| PR 流程 | gh pr、审核闭环、/tmp 临时文件 | —（流程在用户级 skill） | — | [pitfalls/github-pr.md](pitfalls/github-pr.md)、[tasks/pr33-merge.md](tasks/pr33-merge.md) |

## 共享环境与全局资料

- 命令与启动：仓库根 `CLAUDE.md`「常用命令」（`npm.cmd` 前缀、PowerShell、`.venv`、`start.ps1`）。
- 审核闭环规则：用户级 skill `pr-review-loop`（C:/Users/GAO SHEN/.claude/skills/pr-review-loop/SKILL.md）。
- CI：GitHub Actions 双 job（前端 typecheck/build/state-isolation、后端 compileall/分层门禁/pytest）；合并前 `gh pr checks` 全绿。

## 查不到时

先查当前任务中的入口，再在对应源码目录搜索；输出受限标题/路径后读取相关段落。
不要遍历所有 sessions。领域过大时建立二级路由，在这里保留一个入口。

## 已知缺口/迁移

后端/platform/engines/BGM/管理控制台领域无模块卡片（unknown，核实入口 = `CLAUDE.md`「需求落点速查」+ 源码）；无迁移路径（首次初始化）。
