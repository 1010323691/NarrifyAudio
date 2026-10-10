# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

NarrifyAudio 是 Windows 本地有声书制作工作台：Vue 3 + TypeScript 前端、FastAPI API、独立 Worker 进程、PostgreSQL 16、Memurai（Redis 协议兼容）。第一阶段代码治理已实施完毕（治理计划文档 `docs/plan.md` 已由负责人删除，结论与约束已沉淀进本文件和代码注释，历史细节可查 git log）。

业务流水线（长时执行全部在 Worker 进程，API 层没有进程内业务线程）：
文稿上传 → 文本排版/分册（`text.format` / `book.split`）→ LLM 解析（`script.parse`）→ 角色音色（`voices.*`）→ 批量合成（`tts.batch`，TTS 子进程）→ 合并（`tts.merge`，按章节）→ BGM 分析与混音（`bgm.*`，按章节）→ 打包导出（`bgm.package` / 资源中心 `resources.package`）。

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
.\launch\start-data-services.ps1                         # 启动 PostgreSQL + Memurai Windows 服务
.\launch\start.ps1                                       # 全栈：检查数据服务 → 跑迁移 → 启动 API/Worker/Vite
.\launch\stop-data-services.ps1                          # 先停 API/Worker 再停数据服务
```

```powershell
npm.cmd run typecheck            # vue-tsc 前端类型检查
npm.cmd run build                # 类型检查 + 生产构建（dist/）
npm.cmd run build:all            # 前端构建 + 后端 compileall + 分层门禁（lint-imports）
npm.cmd run lint:imports         # 分层门禁单独运行
npm.cmd run test:ci              # 14 个 node:test 沙箱回归合并并行跑（CI 用；下列单项命令仍可单独跑）
npm.cmd run test:state-isolation # 前端状态隔离回归（node:test 沙箱跑 Pinia store）
npm.cmd run test:workbench       # 章节核对工作台组合回归（node:test 沙箱跑 useTextFormatWorkbench 恢复/派生/分页逻辑）
npm.cmd run test:script-parse-workbench # 文本解析工作台组合回归（node:test 沙箱跑 useScriptParseWorkbench 行状态/提交/缓存/竞态）
npm.cmd run test:admin-charts    # 后台图表纯几何函数回归（scale.ts：刻度/折线断点/仪表弧/环图扇区）
npm.cmd run test:voices-workbench # 角色音色工作台组合回归（node:test 沙箱跑 Voices.vue 加载/提交/合并/竞态逻辑）
npm.cmd run test:batch-merge     # 批量合并疑似角色回归（指向树推导/排序/选中展开/新增候选/竞态与版本冲突）
.\.venv\Scripts\python.exe -m pytest backend/tests -n 4 --dist loadscope  # 后端全量测试（4 进程并行，见下方说明）
.\.venv\Scripts\python.exe -m pytest backend/tests/test_script.py  # 单个文件
.\.venv\Scripts\python.exe -m pytest backend/tests/test_script.py -k 名称片段  # 单个用例
```

测试默认跑在 sqlite + 临时存储上（`backend/tests/conftest.py` 覆盖 `NARRIFY_DATABASE_URL` / `NARRIFY_STORAGE_ROOT`），全套件不需要本机 PostgreSQL/Memurai 在运行；少数真实 DB 用例（`test_migrations`、`test_postgres_cancellation_concurrency` 等）带 `skipif`，无 DB 时自动跳过。全量套件用 pytest-xdist 并行跑，worker 数**固定 4**（实测：串行 90s，`-n 2/4/8/12/16` = 61s/37s/61s/57s/71s——套件含大量派生子进程的测试，超过 4 worker 后 CPU 超额订阅，比串行还慢，故不用 `-n auto`）；`loadscope` 不可省——`test_platform` 有用例依赖同文件前序用例留下的模块级 DB 状态，同一文件必须整体留在一个 worker 内按序执行。

后端测试支持 `--shard I/N`（按文件整体分片，CI 用 3 片；按 `backend/tests/shard_weights.json` 里每个文件的实测耗时做贪心均衡，新增文件按用例数估算。测试明显变多/变慢后用 `pytest backend/tests -n 4 --dist loadscope --write-shard-weights` 刷新权重表并随 PR 提交；仅在全量、无筛选（含 --deselect）、全部通过时才会写盘，否则只告警）。新增测试避免真实等待：轮询/超时/锁等待提成模块常量并在测试里 monkeypatch 缩短（例：`task_views.STREAM_POLL_SECONDS`、`api.tts.PREVIEW_LOCK_TIMEOUT`），节流类时钟测试用步进而非逐秒；确需长跑的用例可打 `@pytest.mark.slow`（已注册，用 `-m "not slow"` 排除）。CI 按改动路径决定跑前端/后端 job（`ci.yml` 的 `changes` job）。

提交后端改动前跑完整后端测试套件；涉及前端或共享流程时额外跑 `npm.cmd run typecheck` 和 `npm.cmd run build`。不可逆数据库变更前先备份（历史备份在 `.backups/`）。

## 后端分层契约（强制）

`api → services → platform → core` 单向依赖，由 import-linter 按 `.importlinter` 强制执行，`npm run build:all` 包含该门禁，反向导入直接构建失败（当前无豁免边）。`engines/`、`main.py`、`worker.py` 暂未纳入分层。

- `backend/api/` — FastAPI 路由。`main.py` 中 PLATFORM_ROUTERS 直接注册；6 个 legacy 路由（config/files/bgm/tts/script/music）统一挂 `require_legacy_access` 依赖（认证 + 状态变更请求要求 CSRF）。
- `backend/services/` — 业务编排（项目文件系统、任务视图等）。
- `backend/platform/` — 多用户平台核心：任务注册表、任务生命周期、事务 Outbox → Redis Streams、额度账本、session/安全、存储与迁移。
- `backend/core/` — 配置、路径布局、并发门、`pathio`、`file_lock`、可观测性等基础件。
- `backend/engines/` — 业务引擎（script/tts/merge/bgm/book/text/audio（仅 ffprobe 时长探测）/music/voices）。

## 任务系统

- 19 种任务类型（8 平台直连 + 11 legacy 引擎）的单一事实源是 `backend/platform/task_registry.py`（`task_types.py` 只做 re-export）。
- 提交链路：路由 → `platform/task_submission.py:submit_task_record` → 事务内写 Task + OutboxEvent → Worker 的 `outbox.publish_pending` 投递 Redis Streams（租约 + XAUTOCLAIM 恢复，`recover_database_tasks` 兜底数据库侧遗留任务）→ 两个执行分发器：`platform/task_worker.py:execute_claim`（新类型）与 `platform/engine_task_executor.py:execute_engine_task`（legacy 引擎类型）。
- 任务 HTTP 表面已统一为 `/api/v1/tasks*` 单套（列表/历史/SSE `GET /api/v1/tasks/stream`/取消/重试/批量控制都在 `api/platform_tasks.py` 一个 router；前端 `src/api/tasks.ts` + `durableTasks.ts` 共享它，`stores/task.ts` 以单条多路复用 SSE 承载全应用任务事件）。旧的「legacy SSE 表面 vs v1 轮询」双表面描述已作废，不存在双任务数据源。
- 额度按操作计费（`platform/quota.py` 的 `QuotaHold`）是现行生效路径；任务级 `QuotaReservation` 已退役。
- 工作区绑定：请求级 ContextVar（`main.py` 的 `bind_authenticated_workspace` 中间件，Starlette 可能在不同线程跑同步依赖，故不能在 auth 依赖里绑）+ 任务级配置快照（`core.config`，任务启动时绑定）。

## 需求落点速查（改一个功能，从哪进）

- **改某个流水线任务的行为**（合成/合并/BGM…）：以任务类型字符串（`tts.merge`、`bgm.mix`…）为锚点，`platform/task_registry.py` 的表一行给出计费/权限/executor 绑定；executor 实现按 `legacy_engine` 分两处：`task_worker.DIRECT_EXECUTORS`（8 个新类型）或 `engine_task_executor.ENGINE_BRANCHES`（11 个 legacy 类型），实际算法在 `engines/*`。注册表是分发决策的唯一事实源：分发器按函数表 `DIRECT_EXECUTORS` / `ENGINE_BRANCHES` 路由，`test_task_registry` 钉住表与函数表的一致（影子双跑已于 2026-10-03 清退，见 #34）。
- **新增任务类型**：`task_registry` 表 + 对应分发器的 executor + `platform/task_submission.py` 提交校验 + 前端 `src/utils/taskTypes.ts` / `taskLabels.ts`（类型前缀知识还重复在 `ProjectOverview.vue`，需同步；管理端的服务组归类以后端 `task_registry.task_worker_group` 为准）。
- **UI 页面/交互**：`src/views/X.vue` + `src/router.ts` 注册（项目阶段页面加 `meta: { projectStage: true }`）+ `src/api/` 对应客户端；长时操作一律走任务提交 + 轮询/SSE，不在请求里同步跑。
- **配置项**：项目配置随工程存 DB（`/api/config` 读写活动工作区配置，永不写根模板）；LLM 凭据等由管理控制台管（`api/admin.py` + `platform/system_config.py` 的 `SystemConfig`）；`core/config.py` 的功能默认值由 `platform.system_config` 注册的 provider 供给（分层契约要求 core 不反向 import platform）。
- **文本解析协议（`script.parse` 的 chunk 解析）**：`generation.parse_protocol`（管理端「生成参数」可切）默认 `units`，`json` 为旧协议且是回退路径。`units` = `engines/script_units.py`（纯函数：`segment_chunk` 程序切编号单元 → 模型只输出标签行（台词/`X` 删除/`E` 编辑/`B` 另起/`END`）→ `parse_unit_reply` → `assemble` 机械拼回与旧协议完全相同的 `{speaker,text,instruct}`），编排在 `engines/script.py:process_chunk_units`（缺 END 但自然停笔可接受；标签无效重试，三次失败则**该 chunk** 回退 `process_chunk`）。提示词在 `resources/default_unit_prompts.txt`（`PromptsConfig.unit_*` 可覆盖）。旁白 instruct 不由模型输出，统一用管理员配置的 `narrator_instruct`；`E` 只允许从单元里删去一段说话动词短语（`validate_edit`），`X` 只能删短片段或含网址/水印的行（`delete_max_chars`），不合规一律作废并保留原文；`edit_enabled` / `edit_max_delete_chars` / `unit_max_chars` / `delete_max_chars` 均为管理员项。下游检查阶段、返回字典与落盘格式不变（新增 `unit_*` 读数字段）。检查段里的断句校验与超长段落 LLM 重切在 `units` 协议下同样走编号单元（`script.py:rederive_entries_units`：条目文字程序切单元、模型只出标签、逐条目机械拼回；重切按章节打成一包（一章最多一次调用，装不下时只送最长的几条，其余交机械分段），每包条目文字不超过 `chunk_size`、文字加各条前后文不超过其 2 倍（chunk_size 下限按 1000 计）——引擎单次请求有上下文上限，实测整章打包曾因 `max_context 20000` 被 400 拒绝；同一原则也用于重判批的单个窗口（超过 `min(check_pack_max_chars, 2×chunk_size)` 就对半拆）和 instruct 修复（按同一预算分批，不再整本一次调用）；新增任何「多条合一次调用」的打包都要把前后文算进预算），`json` 协议下保持旧的整段 JSON 重写。重判批（角色匹配检查 / 归属抽样共用的 `_run_rejudge_groups`）会把互不相邻的窗口打包进同一次 LLM 调用（`check_pack_targets` / `check_pack_max_chars` / `check_pack_max_windows`，`check_pack_targets=0` 即关闭打包回到每组一次调用），投票重试只重发未决窗口；边界复核目标由 `boundary_target_window`（默认 2，0 = 不收窄）收窄。真实 LLM 对比用 `scripts/ab_parse_protocol.py`（按文字对齐算说话人一致率、两协议各自的归属抽样随机桶读数；注意：受 GPU 调度托管时请求会被改写到平台 LLM，且别直接杀进程——会留下孤儿 GPU 许可使调度器进入 ERROR）。旧测试默认钉在 JSON 协议（`conftest.legacy_json_protocol`），单元协议测试见 `test_script_units.py`。
- **角色合并 / 疑似角色提示**：指向关系不再实时重算，持久化在 `04_voice_profiles/role_links/<范围>.json`（`core/role_links.py`：links/vetoes/new_candidates/records + 乐观锁 `version`，范围 = `__all__` 或单个解析文件）。`engines/role_merge.py` 是唯一写入口：批量合并/撤销/忽略都在 `foundation-publication.lock` 内整体读改写，多文件写失败会回滚；撤销前校验台词文本哈希与归属。匹配规则与命中依据在 `core/role_hints.py`（`suggest_role_links` / `rematch_one`）。接口在 `api/tts.py` 的 `/voices/merge-graph|merge-batch|merge-undo|link-veto|merge-review-seen`，旧 `merge-speakers` 复用同一实现；列表的 `alias_of`/`alias_basis` 读同一张表。前端入口 `views/voices/BatchMergeDialog.vue` + `composables/useBatchMerge.ts`（纯推导在 `batchMergeGraph.ts`）。
- **项目保质期 / 回收站期限**：管理员在「系统配置 → 账户与客户端」设置 `retention.projects`（`platform/system_config.py:project_retention`，`/api/v1/admin/settings/retention`）：`project_ttl_days`（创建满 N 天整体永久删除，默认 30，0 = 不过期）与 `trash_days`（回收站保留，默认 7）。清理每个本地日（0 点起，错过则 Worker 下次起来补跑）只跑一次：`worker._retention_pass`（`retention.last_run` 记当天已跑；维护协调器每分钟问一次，所以该函数一律返回 incomplete 让协调器继续轮询），先回收站后自然过期，再做次日复查。「默认工作空间」跳过；仍有未完成任务的项目照常删除（同一轮内先对任务发取消、最多等 `CANCEL_GRACE_SECONDS`，再 `force` 删除；Worker 把任务行消失视为取消）；每次永久删除在 `project_purge_records` 留一条回执（只有 id/存储键，无名称），次日及以后的一轮 `services/project_purge_audit.py:verify_purged_projects` 重查工作空间、`.deleting-*`、`.resources` 与全部按项目挂的表，发现残留就清掉并写进回执 `leftovers`，复查干净才标记 `verified_at`（已核验回执保留 30 天）；删除走 `services/projects.py:permanently_delete_project`，除工作空间目录（含项目日志）和全部数据库行外，还会清掉工作空间之外的 `.resources/<owner>/<project>/` 快照与该项目 `resources.package` 的导出目录。新增任何按项目挂的数据（表或工作空间外的文件）都要同步进这个函数，否则过期后会留下痕迹。恢复时超过任一期限都会拒绝（410）。
- **额度/计费**：`platform/quota.py`（按操作 `QuotaHold`）。
- **DB 结构变更**：`backend/migrations/` 加 Alembic 迁移（链从 0001 起，head 须与 `platform/models.py` 一致；不可逆变更先备份）。
- **并发/并行度**：`core/concurrency.py` 的 `merge_concurrency_limit()` 按逻辑 CPU 数的一半、限制在 1～4 槽；`merge_gate()` 由音频合并与 BGM 混音共用，Worker 启动等量的专用执行线程领取 `tts.merge` / `bgm.mix`，主循环排除这两类任务。正式执行链路的 `_workspace_engine_lock` 对具名合并/混音使用项目共享锁及章节排他锁，允许不同章节重叠，仍与其他项目写任务互斥，并持锁覆盖产物发布/提交；未指定音频包的旧式合并继续使用项目排他锁。LLM gate 由 Worker 的 parse 协调器每轮按管理端 `parse_worker_concurrency` 配置经 `set_concurrency` 动态调整（默认 4、上限 32，parse worker 池随并发伸缩、上限 64 槽），LLM 解析可并行至 32。每次 LLM 调用先过 `platform/gpu_scheduler/admission.py:gpu_permit` 的数据库许可（轮询 + 先来先放行，每个被轮询到的等待者在同一轮内不再受「只放行队首」限制；同进程内有请求结束会直接唤醒队首等待者，唤醒只花在正睡着的等待者身上（暂停任务停在 `check()` 里的队首不吞唤醒），放行后若仍有空闲名额再继续唤醒；LLM 轮询间隔 0.05→1s，兜底跨进程；每轮轮询先做无锁只读预检（`_llm_precheck`：没有空闲名额 / 调度器不在服务 LLM / 排位在空闲名额之外就直接睡，不进 `host_lock` 事务；队首附近短退避、靠后者睡 1s 并靠唤醒），放行仍只在锁内判定；`audiobook.llm_trace` 日志每次调用一行 `wait`/`run` 秒数及放行时的 `active`/`limit`/入队 `rank`）——曾因「一轮只放行最老的一个 + 最长睡 2 秒」使推理服务 8 槽平均只有 5 个在跑、服务端无排队，改动前后对比看推理端 `requests.jsonl` 的 `scheduler.running/waiting`。调整并行度时同时检查 Worker 调度槽位、项目/章节锁和引擎侧的 acquire 点（`engines/bgm.py`、`merge.py`、`music.py`、`script.py`、`voices.py`）；合并/混音槽位是进程级限制，多个 Worker 进程不会共用该门禁。
- **工作区路径/文件产物**：布局在 `core/paths.py`，存储对象在 `platform/storage.py`；可移植文件名规则统一由 `core/filenames.py` 实现。存储侧 `safe_display_name` 先取 basename，音频包侧 `package_stem` 把整个包名作为 stem 并为音频扩展名预留长度。`legacy_storage_name` 与 `package_aliases` 保留历史拼写，仅用于兼容查找/失效删除，不作为新产物身份；账号目录通过 `storage_username` 固定使用历史拼写。发布时原始名与磁盘名分开保存，历史清洗名重新发布必须核验同类任务及来源文件身份。

## TTS 子进程隔离

TTS 引擎（`tts-engine/tts_worker.py`，约 2300 行）是 one-shot 子进程，FastAPI 进程永不 import torch/模型代码。通信为 CLI 参数 + 磁盘 JSON + stdout 行协议；退出码 124 = 看门狗超时（`engines/tts.py:run_tts_subprocess` 将其映射为可降批重试信号，而不是整任务失败）。`AUDIOTTS_WORKER` 环境变量可覆盖 worker 脚本路径。FFmpeg 需在 PATH。

## 前端架构

- 哈希路由，两个互不链接的门面：用户工作台（`/`，`MainLayout`，`requiresUser`）与管理控制台（`/admin`，`AdminLayout`，`requiresAdmin`），各有独立登录页与角色守卫（`src/router.ts`）。`meta.projectStage` 视图在无活跃项目时重定向到 dashboard。
- `src/api/client.ts` 是唯一 HTTP 入口：cookie session + 从 `narrify_csrf` cookie 取 `X-CSRF-Token`。开发走 Vite `/api` 代理；生产默认同源 `/api`（FastAPI 托管 `dist/`，GET catch-all 回 SPA shell，未知 `/api/*` 保持诚实 404）。分域部署在**构建时**设 `VITE_API_BASE` / `VITE_CSRF_COOKIE_NAME`，改完需重新构建。
- Pinia stores（`src/stores/`）带竞态保护：reset 必须使在途请求失效（旧账号的配置/创建结果不得复活）。这些场景由 `scripts/test-state-isolation.mjs`（`npm run test:state-isolation`，node:test + vm 沙箱加载真实 store 代码）钉住——改 store 行为时该文件是回归门禁。章节核对工作台的组合逻辑（`src/composables/useTextFormatWorkbench.ts` 的恢复泵 / canEnterParse / 过滤分页 / 竞态丢弃）由 `scripts/test-textformat-workbench.mjs`（`npm run test:workbench`，同款沙箱）钉住；文本解析工作台（`src/composables/useScriptParseWorkbench.ts` 的行状态双轴 / 提交门控 / 预览缓存 / 跨项目竞态）由 `scripts/test-script-parse-workbench.mjs`（`npm run test:script-parse-workbench`，同款沙箱）钉住；角色音色工作台（`src/views/Voices.vue` 的加载 / 克隆提交 / 合并弹窗 / 竞态丢弃）由 `scripts/test-voices-workbench.mjs`（`npm run test:voices-workbench`，同款沙箱）钉住。
- 管理控制台：`views/Admin.vue` 只按 `?tab=` 分发到 `views/admin/*.vue`（各页一个 `useAdminLoader` 实例，承载竞态/失效/轮询语义，由 `scripts/test-admin-console.mjs` 钉住）；图表统一走 `components/admin/charts/`（手写 SVG 组件，零图表库依赖；视图只产出 spec，`AdminChart.vue` 按 `kind` 分发到 TimeSeries/Bar/Donut/Gauge/Heatmap/Sparkline；配色是引用 `admin.css` 中 `--viz-*`/`--status-*` 的 CSS 变量，亮暗随 `.dark` 自动切换；纯几何函数在 `scale.ts`，由 `scripts/test-admin-charts.mjs` 钉住）。**新增或修改任何图表前先读 `docs/MONITOR_CHARTS_DESIGN.md`（项目图表设计指南：零依赖手写 SVG、设计令牌配色、折线/仪表/进度条/环图的写法与亮暗主题约定），沿用同一套风格，不引入图表库。**趋势数据来自 Worker 每 30 秒写入的 `system_metric_samples`（`platform/metrics_sampler.py`，保留 7 天）与任务表 / 额度账本聚合（`services/admin_analytics.py`）；API 延迟序列是 API 进程内存口径。
- `dist/` 是构建产物，不手改。

## design 目录（计划与开发目标文档）

所有计划文档、开发目标文档一律在仓库根目录的 `design/` 下创建。该目录本身存在于 git 仓库，但仅 `design/.gitkeep` 入库，其余内容均被 gitignore，属本机临时文档，不提交、不推送。

`docs/` 则是项目设计说明（部署、运维、开发指南、架构说明等），全部随仓库提交并推送远程，不做忽略；临时的计划/目标类文档不要放进 `docs/`。

## 运行时数据与配置

- `storage/`、`config/`、`logs/`、`.narrify/`、`music_library/`、`.backups/` 均为本机运行时/用户数据，已 gitignore——不当源码清理，也不提交。`music_library/` 是跨工程共享的全局音乐库。
- 根目录 `setting.json` 是已跟踪的默认配置模板及运行时工作空间指针；提交前必须清空工作空间路径，不提交真实凭据。工作空间配置为 `config/setting.json`。
- `.env`（gitignored）：`NARRIFY_DATABASE_URL`、`NARRIFY_REDIS_URL`、bootstrap 管理员账号。`postgres` 管理员密码仅供人工 psql 运维，不入 `.env`。LLM 凭据与制作参数在应用内设置页配置，不写进代码/文档/前端。

## 约定

- Python 4 空格缩进、`snake_case`；TS/Vue 2 空格、组件 `PascalCase`、函数变量 `camelCase`。优先复用已有的文件、任务管理、通用工具函数。
- 提交信息简洁、描述可观察到的改动、一次提交聚焦一个变更（仓库历史中英文皆有）。
- 涉及路径处理与子进程逻辑（ffmpeg、TTS worker）要特别谨慎；不提交 API 密钥、本机工作空间路径、生成的音频、日志或虚拟环境。
