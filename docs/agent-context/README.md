# 项目交接入口
协议：context-protocol/v2
交接根：docs/agent-context/
更新时间：2026-10-01T19:45+09:00
工作目录约定：git 仓库根（同时含 CLAUDE.md 与 package.json 的目录）

## 先读这些

1. [项目概览](PROJECT.md)：目标、架构和一级功能领域。
2. [当前停止点](CURRENT.md)：本次结果、活动任务、验证范围和下一步。
3. [领域路由](ROUTES.md)：按任务定位模块、命令、坑和设计书。

## 按需资料映射

| 资料 | 路径/入口 | 关键词与适用范围 |
|---|---|---|
| 项目主设计书（命令、分层契约、任务系统、前端架构、运行时数据） | 仓库根 `CLAUDE.md` | 检索「常用命令」「后端分层契约」「需求落点速查」「TTS 子进程隔离」 |
| PR 审核闭环流程（审核循环规则） | 用户级 skill `pr-review-loop`（C:/Users/GAO SHEN/.claude/skills/pr-review-loop） | 审核闭环、Verdict、复审、再裁决 |
| 活动任务：PR #33 合并待办 | [tasks/pr33-merge.md](tasks/pr33-merge.md) | PR 33、gh pr merge、draft、删分支 |
| 决策：角色配音背景 inert 拆独立项 | [decisions/D-20261001T104021Z-r4c6a1.md](decisions/D-20261001T104021Z-r4c6a1.md) | inert、a11y、角色配音子窗口 |
| 模块：角色配音 overlay 键盘焦点 | [modules/voices/overlay-focus.md](modules/voices/overlay-focus.md) | overlayKeydown、Tab 陷阱、in-flight |
| 坑：PR 评论临时文件路径 | [pitfalls/github-pr.md](pitfalls/github-pr.md) | --body-file、/tmp、Git Bash |

## 保存约定

- 模块现状、活动任务、正确命令、失败经验、长期决策分别维护；其他位置引用 ID/路径。
- CURRENT 的 complete 表示交接保存完整，不表示开发完成或已探索整个系统。
- 同一目录串行维护交接；不同 worktree 不自动同步资料。
- 记录文件默认随项目保存；实际提交/备份情况见 CURRENT，skill 不自动提交。

## 已知资料缺口

首次初始化于 2026-10-01：无后端/platform/engines 模块卡片与命令卡片（常用命令与分层契约以 `CLAUDE.md` 为准，本目录不复制）；本次检查范围 = 本交接目录、`CLAUDE.md`、git/PR 状态、角色配音相关源码。
