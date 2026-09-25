# NarrifyAudio 代码治理方案（第一阶段成果 + 实施计划）

> 本文档由第一阶段只读治理审查整理而成，取代此前的早期迁移规格（旧版可从 `git show d053c05^:plan.md` 取回，d053c05 为将其移入 docs/ 的提交；其中仍有效的实施约束已并入文末"持续生效的约束"）。
>
> **状态**：第一阶段（调查、分析、论证、规划）已完成；8 项待确认事项已由项目负责人裁决完毕（见第六节）；5b（legacy 业务路由整体收敛进 v1）经裁决**暂缓**，不在本轮范围。
>
> **当前阶段（2026-09-26）**：批次 0–6 **已全部实施完成**（origin/main..HEAD 43 提交，至 b882e54）；对那 43 个提交的全量审核留下 1 高 + 6 中 + 13 低指摘，其**收口修复已完成**（13 个可独立 revert 的提交，hash 见文末"验收记录"）。5b 维持暂缓。
>
> **异议核实记录（2026-09-25）**：负责人对本 plan 提出 8 条异议，已逐条对照代码核实并**全部采纳**（详见三/四/六/八节对应修订）：① Q7/A1 迁移 0015 只删 `quota_reservations` 表，`reserved_units`（QuotaHold 实写）与 `frozen_units` 账户列保留；② Q2 BGM 读-改-发布改跨进程锁保护（`music.py:455` 模式，两侧同改）；③ Q5 撤销"`safe_display_name` 未被使用"，收窄为 tts_manifest 模块内两处内联去重；④ A-1 降为 P3 残留参数清理（无 UI 勾选项）；⑤ Q4 修正线程占用表述，批次 1 先行最小修复（非占用等待），5a 仍退役该表面；⑥ Q11 `formatBytes` 4 副本互不等价，移出批次 0，批次 2 参数化保真统一或保留现状；⑦ Q17 撤销"包级循环/eager 化即炸"断言，改列两条单向反向边；⑧ Q15 diff 测试只比共同业务参数，`stream`/`stream_options` 豁免，extra_body 不对称保留为修复决策项。
>
> **第二轮异议（2026-09-25，7 条）**：前 6 条采纳并已落入正文——`estimated_units` 参与幂等哈希（**后经 2026-09-26 裁决改为彻底删除**：请求面 + `estimate_legacy_units` 删除，哈希键固定为常量 0 + 回放回归，收口 f0d05f8，见 4.2/A1）；BGM 锁扩展覆盖 artifact 发布/回滚/恢复全生命周期，含"worker 发布 → API 修改 → worker 回滚"场景测试（Q2/批次 1，回滚守卫由收口 07fb247 补齐）；SSE 同步操作（认证 + 每轮事件查询）封装 `anyio.to_thread.run_sync` 交线程池、Session 在封装内开闭，5s 认证缓存只限认证频率的表述修正（Q4/批次 1）；Q15 改述为"参数扩展能力不一致"（主解析流式与非流式分支均不传 `enable_thinking`，截断归因不成立），补透传与默认关思考分开决策（Q15/批次 3）；`format.ts` `i === 0` 对应 KB（非 B）（Q11）；`core/config.py:250` 为裸延迟导入、不推定设计意图（Q17）。第 7 条（"eager 化在 `core.paths` 上成环、启动即炸、延迟导入是断环必需"）**经完整导入链 + 实证复现排除**：eager 链零回边指回 `core.config`；预置半初始化 `core.config` 后 import `platform.feature_config` 成功；真正的环对是 `core.config(:31)` ↔ `core.paths`，由 `core/paths.py:117/139` 局部导入断开，与 `config.py` 两处函数级导入无关（详见 Q17）。
>
> **执行纪律**：每批独立可回滚（一批一提交范围，回滚 = revert 该批提交）；前置条件为上一批验收通过且工作区干净；后端改动完成后运行完整后端测试，涉及前端时运行 `npm run typecheck` 与 `npm run build`；不可逆数据库变更执行前先备份。

---

## 一、项目概览

**产品定位**：NarrifyAudio 是运行在 Windows 本机的有声书制作工作台，已从单用户本地工具完成向多用户 Web 服务的基础迁移（PostgreSQL 持久任务 + 独立 Worker + 额度账本），当前目标是代码治理：清理死代码、理顺职责、修正命名与设计。

**业务流水线**（已验证，依据 `backend/platform/task_types.py`、`src/router.ts`、各引擎实现）：

上传文稿 → 文本排版/分册（text.format / book.split）→ LLM 解析（script.parse）→ 角色音色（voices.foundation / voices.clone）→ 批量合成（tts.batch，TTS 子进程）→ 合并（tts.merge）→ 分集（audio.silences / audio.cut）→ BGM 分析与混音（bgm.*）→ 打包导出（audio.zip / audio.export）。

**技术栈与进程模型**：

| 组件 | 技术 | 关键文件 |
|---|---|---|
| 前端 | Vue 3 + TypeScript + Pinia + vue-router + Vite + Tailwind（shadcn 风格） | `src/`（约 1.3 万行） |
| API | FastAPI（端口 8642），托管 `dist/` 构建产物 | `backend/main.py` |
| Worker | 独立进程：事务 Outbox → Redis Streams，租约 + XAUTOCLAIM 恢复 | `backend/worker.py`、`backend/platform/task_worker.py` |
| TTS | 隔离子进程（Qwen3-TTS / qwen-tts + torch），CLI + 磁盘 JSON + stdout 行协议，退出码 124 = 看门狗超时 | `tts-engine/tts_worker.py`（2490 行） |
| 数据库 | PostgreSQL 16 + Alembic（15 个迁移，head 与 models 一致） | `backend/migrations/` |
| 缓存/队列 | Memurai（Redis 协议兼容） | `worker.py`、README |

**规模**：248 个受跟踪文件；生产代码约 5.9 万行（backend Python 约 3 万行；前端约 1.3 万行；tts_worker 2490 行）；测试约 2 万行（32 个文件，`from backend…` 导入 315 处全部可解析）。

**任务系统核心事实**（已验证，含交叉核对修正）：

1. 全部 19 种任务类型的长时执行都在 Worker 进程；API 层无"进程内起线程跑业务"的代码。
2. `/api/tasks*`（legacy 表面）是**持久化任务数据库的响应适配层**（`api/tasks.py:1-6` 自证），不是旧内存任务系统。前端 legacy SSE（`stores/task.ts`）与 v1 轮询（`persistentTasks.ts`）消费的是**同一数据源的两种 HTTP 表面**——不存在双任务数据源。
3. 提交统一入口：`api/task_submission.py` → `services/tasks.py:submit_task_record` → 事务内写 Task + OutboxEvent → worker 投递 → 两个执行分发器（`platform/task_worker.py:execute_claim` 6 种新类型；`platform/engine_task_executor.py` 13 种 legacy 引擎类型）。19/19 类型有实现，无孤儿。
4. 额度：按操作计费（`platform/quota.py`）为现行生效路径；任务级 `QuotaReservation` 为休眠子系统（已裁决退役）。
5. 工作区绑定：请求级 ContextVar（`main.py:106-131`）+ 任务级配置快照（`bind_task_config`），无"执行前切换全局 workspace"。

**当前健康面（保留不动）**：15 个迁移版本链完整且被测试证明承力；`task_control.py`、`pathio.py`、`file_lock.py`、`observability.py` 全部在用；`book.py`/`text.py` 职责边界有文档化划分；启动/构建脚本有效；`taskLabels.ts` 与后端任务类型 19/19 对齐；前端 15 个 api 文件 98 个导出在 ef89a79 清理后无死导出。

---

## 二、审查覆盖范围

**已检查**（7 条并行审查线，逐文件阅读 + 交叉核对 + 主审抽查复核）：

| 模块 | 覆盖方式 |
|---|---|
| `backend/main.py`、`worker.py` | 全文阅读 + 主审复核 |
| `backend/core/*`（config、paths、pathio、tasks、concurrency、request_context、logging_setup、observability、file_lock） | 逐文件全文阅读 |
| `backend/platform/*`（task_types、task_worker、engine_task_executor、task_state、task_context、legacy_tasks、legacy_files、deps、storage、bootstrap、outbox、models、quota、feature_config 等）+ 迁移 0001–0015 全链 | 逐文件全文阅读 |
| `backend/engines/*`（script、tts、tts_batch、tts_manifest、voices、book、text、audio、merge、bgm、music、llm_transport 等） | 逐文件全文阅读 |
| `backend/api/*`（18 个路由文件，68 个端点） | 端点全枚举：前端字符串、测试、脚本、文档、.env、app.json 模板逐一核对调用方 |
| `tts-engine/tts_worker.py`（2490 行） | 全文阅读（模式分发、子批重试、stdout 协议、看门狗） |
| `src/`（api 15 文件、6 stores、3 composables、4 utils、16 视图、30 组件、router） | 每个导出/组件逐一核对调用方；任务类型字符串与后端逐一比对 |
| 周边：`scripts/`、启动/停止脚本、AGENTS.md、README.md、docs/、测试全库 | 文件级检查 + 测试导入可达性全量核验 |

**判定规则**：每个"无引用"结论在判定前均核验——动态 import、字符串引用（SSE URL、任务类型、配置键）、DI 与自动注册、框架约定（FastAPI 路由、Alembic 链）、CLI 入口、外部调用方（前端字符串/脚本/文档）、构建脚本、环境配置。内部公共接口无内部使用时归"需进一步验证"，不得直接判死。

**未检查（及理由）**：`dist/`、`node_modules/`、`.venv/`、`__pycache__/`（构建产物/运行时缓存，gitignore）；`storage/`、`.narrify/`、`music_library/`、`config/`、根 `app.json`（运行时用户数据）；运行时行为验证（受第一阶段只读约束，相关结论标注"需真实触发确认"）；仓库外契约（LLM 服务商、旧外部 Python 集成——已由项目负责人裁决为"无外部访问"）。

---

## 三、问题清单

优先级：P1（本轮应修）> P2（下一轮）> P3（可选）。

| # | 类别 | 位置 | 证据 | 影响 | 处置 | 优先级 | 置信度 |
|---|---|---|---|---|---|---|---|
| Q1 | 缺陷 | `tts-engine/tts_worker.py:1833-1834`（`_run_batch` 内嵌 `run_planned_group`，:1793-1843） | `log()`（L157）只收 1 参，调用处传 2 参（隐式拼接 + `"WARNING"`） | 多角色 TTS 子批失败且 `len(half_rows)>1` 时，except 块内抛 TypeError：降并发重试不执行，原异常被替换，整段批次失败 | **批次 0 修复**（见批次 0 细则：日志单参化 + 提升循环为可注入依赖的模块级函数 + 失败注入回归测试） | P1 | 高 |
| Q2 | 缺陷 | `api/music.py:650`（`_propagate_chapter_analysis`，调用点 :673 标签改名、:714 标签删除） | 直接 `f.write_bytes` 写 `08_bgm/chapter_music_analysis.json`，绕过 `engines/bgm_storage.py` 的 `_BGMS_LOCK`（L13，**进程内** `threading.RLock`）与原子发布 | 进程内锁无法协调**独立运行的 API 与 Worker**；且原子写入只保证最后一步原子，读→改→发布**全过程**不受保护，旧快照可覆盖新数据（丢失写窗口） | **批次 1**：改**跨进程**保护（复用 `engines/music.py:455` 的 `RLock` + `exclusive_file_lock` 模式，覆盖完整 load→修改→发布）+ 原子发布；worker 侧 `bgm_storage` 与 API 侧 `_propagate_chapter_analysis` 两侧同改。**收口修复**：回滚侧补内容守卫——worker 失败回滚恢复共享缓存前，须在同一把跨进程锁内比对文件指纹仍等于本任务发布的字节，并发写者（API 标签传播/手动编辑）胜出则跳过恢复（指摘 M1，收口提交 07fb247，含"worker 发布 → API 修改 → worker 回滚"真子进程场景测试） | P1 | 高 |
| Q3 | 健壮 | `api/music.py:234` `await file.read()` 无大小上限（端点 `require_admin`，:212） | 对照 `files.py:108` 有上限且流式 | 管理员上传超大文件可耗尽 API 进程内存（非通用 DoS，已定级修正） | **批次 1**：流式落盘 + 上限 | P2 | 高 |
| Q4 | 资源 | `api/tasks.py:263/328` 同步生成器 SSE 内 `time.sleep(0.5)` | Starlette 按生成器**每次迭代**借用线程池 worker；`time.sleep` 等待期间占用该 worker，稳态下每连接近似持续占用一个 worker（认证检查走 5 秒缓存窗，单次迭代开销有限，主要压力在占用线程数） | 多标签页线程池耗尽风险 | **批次 1 最小修复**：循环内 0.5s 轮询睡眠改**非占用等待**（生成器协程化、`await asyncio.sleep`——同步生成器内不存在不占线程的等待，协程化即最小改法；循环逻辑、5s 认证缓存、端点表面均不变）→ **5a 根治**（legacy 表面退役）。**收口修复**：v1 聚合/单任务两条 SSE 体内剩余的同步 DB 单元（初始快照、5s 认证复查、0.5s 轮询查询）经 `anyio.to_thread.run_sync` 交线程池（自开闭 Session 的模块级同步函数），慢查询不再冻结事件循环上其它连接（指摘 M3，收口提交 3f883cf，含"单元在非事件循环线程执行 + 循环心跳不饿死 + 生成器关闭后无排队泄漏"测试） | P2 | 高 |
| Q5 | 一致性 | `tts_manifest.py:209`、`:277-278` 两处相同内联副本（替换 Windows 保留字符集、`.strip()`、兜底 `audiobook`、无截断）；`safe_display_name`（`platform/storage.py:34-37`）**生产在用**（约 15 处：工作区路径、对象键、任务产物路径：`admin/files/music/projects/text/legacy_files/engine_task_executor/task_context`），且规则**不等价**（`[^\w.()\- ]+`→`_`、`strip(" .")`、180 字符截断、兜底 `upload.bin`） | 全库 grep + 规则逐行比对 | 两处内联副本可漂移 → 产物路径不一致；两规则不等价，强行统一会改变现网路径 | **批次 2**：tts_manifest 两处内联收敛为模块内单一私有函数（行为不变、注明与 `safe_display_name` 的规则差异）；**不**与 `safe_display_name` 合并 | P2 | 高 |
| Q6 | 重复 | `ACTIVE_TASK_STATUSES` 4 处（`legacy_tasks.py:23`、`services/projects.py:10`、`services/admin_storage.py:13`、`api/project_resources.py:43`）；归属校验 4 处（`projects.py:44`、`project_resources.py:20`、`tasks.py:181`、`task_submission.py`）；task_type→模块映射 2 处（`admin.py:715`、`tasks.py:45`）；重试门禁 2 处（`task_submission.py:51-73` vs `admin.py:602-632` 逐行重复）；文件下载 2 种（`platform/file_response.py:18` vs `music.py:287`） | 逐一核对 | 状态/权限规则改动需同步多处 | **批次 2**：各收敛单一实现 | P2 | 高 |
| Q7 | 休眠子系统 | 任务级额度预留 `platform/task_state.py:49-120`；7 处空转调用（`task_worker.py:185,199,936,974,996,1123,1133`、`services/tasks.py:40`）；`frozen_units` 0 写点（仅展示面 `admin.py:373`、`api/quota.py:18`）；重试门禁 `api/task_submission.py:58-60`、`admin.py:611` 永不触发。注意：`reserved_units` **不是**恒 0——按操作计费 `QuotaHold` 实写该字段（预留 `quota.py:86`、消费 `:132`、释放 `:162`），不可随表删除 | `quota_reservations` 表 0 个生产构造点（唯一构造在 `tests/test_postgres_cancellation_concurrency.py:81`） | plan.md 早期要求的任务级冻结语义未生效；按操作计费（QuotaHold）才是实际路径 | **批次 4 退役**（裁决 A1；迁移 0015 **只删 `quota_reservations` 表**，两个账户列与约束保留） | P1（已裁决） | 高 |
| Q8 | 死代码 | `core/tasks.py`（611 行内存 TaskManager） | 生产 0 引用；仅 8 个测试文件；docstring 所称外部集成已由负责人确认不存在 | 维护负担 + 误导（现任务系统在 `platform/`） | **批次 0 删除**（裁决 A2）；8 个测试文件按侦察结果拆分处置（2 删 6 迁，见批次 0 细则） | P2（已裁决） | 高 |
| Q9 | 前端/架构 | legacy SSE 表面（`src/api/tasks.ts` + `stores/task.ts`）与 v1 表面（`persistentTasks.ts`）并存；v1 SSE `platform_tasks.py:46` 前端未用，durable 侧用 500ms 轮询 | 同一数据源双 HTTP 表面（api/tasks.py:1-6 自证） | 认知负荷；轮询浪费 | **批次 5（5a）退役路线**（裁决 A3） | P1（已裁决） | 高 |
| Q10 | 前端缺陷 | `Settings.vue:122` `settings.config = result.config` | 管理员保存**根**平台配置后写入**项目**配置 store（后端两通道是不同存储：`api/config.py:34-51`"配置随工程"，admin 写根模板）；且视图直接突变 store，绕过竞态保护 | admin 保存后 `useProjectGate`（依赖 `settings.config?.paths?.working_dir`）与默认参数读到根配置，直到重载 | **批次 5**：store 区分 root/project，admin 保存不写项目 store | P2 | 高 |
| Q11 | 重复 | 前端同形拷贝：500ms 轮询 2 份（`useDurableTaskWait.ts:27` 与 `AudioSplit.vue:164-194`）；label 派生算法 3 份（`BGM.vue:109-125`、`MusicLibrary.vue:241-267`、`Merge.vue:63-94`）；任务类型前缀知识 3 份（`taskLabels.ts`、`ProjectOverview.vue:26-34`、`Admin.vue:68-73`）；`formatBytes` 4 份（**互不等价**：空值文案 `未采集`（Usage.vue:35、Admin.vue:80）/ `0 B`（MyResources.vue:59）/ `—`（format.ts:15）；零/负值处理不同（Admin.vue 负值照显、MyResources 非有限或 ≤0 → `0 B`）；小数位规则不同（`format.ts` 中 n≥100 或 `i === 0`（即 **KB** 档；B 走 `<1024` 早退）时 0 位，与其余三份的阈值/规则各异）） | 逐一逐行比对 | 后端新增类型/改 label 需同步多处（`ScriptParse.vue:207-208` 注释已明示脆弱）；formatBytes 直接合并会改变显示行为 | 轮询/label/前缀 **批次 2** 提取 composable/工具；`formatBytes` 改 **批次 2**：仅按调用点语义参数化统一（空值文案、零值、小数规则逐点保持），合并前后逐调用点比对显示值不得变化；若无法参数化保真则保留现状 | P2 | 高 |
| Q12 | 接线缺口 | `core/concurrency.py:85-101` `set_concurrency`/`set_merge_concurrency` 生产 0 调用；`gate()`/`merge_gate()` 永久 limit=1；`merge.py:242` 注释过期 | 全库 grep | LLM/BGM 分析实际串行化 | **批次 4 按裁决 A4**：删 resize 表面，gate 恒 1 保留，修注释 | P2（已裁决） | 高 |
| Q13 | 休眠配置 | `core/config.py:63` tts.enabled、`:68` tts.speaker 0 读取；`.env` POSTGRES_PASSWORD 代码 0 读取；app.json 模板 6 个已退役键被 `test_config.py:48-63` 钉扎 | 全库 grep | 配置表面与行为不符 | **批次 0**（tts 字段、模板键、POSTGRES_PASSWORD，裁决 A8） | P3（已裁决） | 高 |
| Q14 | 死模式 | `tts_worker.py` custom/design/clone 三模式（约 350 行，L1044/L1115/L1193 一带） | 后端只派发 batch/design-batch/merge；CLI choices L2397-2398；负责人确认无手动 CLI 用法 | 约 350 行不可达推理路径 | **批次 4 删除**（裁决 A5），实施前 grep 脚本/文档零引用 + TTS 冒烟 | P2（已裁决） | 中→高（已裁决） |
| Q15 | LLM 传输 | `llm_transport.py:24`（共享非流式）vs `script.py:590` `_llm_chat_completion_stream`（私有流式）：共同业务参数构造重复（model/messages/temperature/top_p/presence_penalty/max_tokens/top_k/min_p/banned_tokens）；`stream`/`stream_options` 是流式合理差异；流式双生无 `extra_body` 透传（非流式有，且 400/422 单重试回退）——bgm/music JSON 路径可传 `enable_thinking: False`（`bgm.py:1179/1361`、`music.py:711`），主解析（流式，`script.py:984/1651`）不能 | 逐行比对 | 潜在行为差异 + 双份维护 | **批次 3**：收敛单一传输层；diff 测试**只比较共同业务参数**（`stream`/`stream_options` 豁免）；extra_body 不对称保留为显式修复决策项（是否给流式路径补透传属行为变化，批次 3 执行时裁决） | P2 | 中 |
| Q16 | 越层 | `engines/tts_batch.py:31-60` 导入 `tts_manifest` 14 个私有符号；`api/tts.py:766,858,945`、`api/bgm.py:797,1530` 调用 `tts_batch._build_segments`（私有）；`api/bgm.py:63,1172,1354`、`api/music.py:45,701` 从 `script.py` 反向导入 `llm_json_with_retry` | 导入清单核对 | 跨层私有耦合，重构牵一发动四处 | **批次 3**：符号提升为公开 API / 移动位置 | P2 | 高 |
| Q17 | 越层（单向反向边） | `platform/legacy_tasks.py:14`（模块级）→ `services.tasks`（platform→services 反向）；`core/config.py:241/250`（函数级）→ `platform.feature_config`（core→platform 反向；两处中一处有 try/except 容错，另一处为裸延迟导入，不推定其设计意图）；engines→platform 额度延迟导入（保留） | 完整导入链 + 实证复现：eager 链 = `core.config → platform.feature_config → {platform.database → platform.config → core.paths → core.request_context，platform.models}`，**零回边**指回 `core.config`（`core.paths` 顶层仅依赖 `core.request_context`，stdlib）；真正的环对是 `core.config(:31 from .paths import …)` ↔ `core.paths`，由 `core/paths.py:117/139` 的函数级局部导入断开（注释自证 "local import to avoid a cycle"），与 `config.py` 两处函数级导入无关；复现实验（预置半初始化的 `backend.core.config` 进 `sys.modules`——eager 导入执行中的最坏状态——再 import `platform.feature_config`）**成功**。"eager 化在 `core.paths` 上成环、启动即炸"据此排除；`services/tasks.py` 不 import `legacy_tasks` | 两条单向反向边违反目标方向（架构风险，非功能性故障） | **批次 3**：固定方向 `api → services → platform → core`（两条反向边按移动/下沉修正）+ import-linter 规则固化 | P2 | 高 |
| Q18 | 前端命名 | 3 处 `const workspace = useProjectStore()`（`MainLayout.vue:8`、`MyResources.vue:20`、`useDurableTaskWait.ts:12`）；`Merge.vue:42` `const project = usePipelineStateStore()` | 逐一核对 | 同名异物，未来撞名 | **批次 2** 纯改名 | P3 | 高 |
| Q19 | 文档/注释过期 | `music.py:616-620`（已删端点）、`api/bgm.py:340-342`（"同步不经任务系统"实为 durable）、`api/bgm.py:592-597`、`api/audio.py:199-201`（docstring 与实际不符）、`api/bgm.py:241`"legacy callers"、`logging_setup.py:3-6`、`alembic.ini:4` 死 change-me（`env.py:13` 无条件覆盖）、`foundation_schema.py:1` 过期 commit 引用、`useProjectGate.ts:4-8` 与 `ProjectGateAlert.vue:22-25`"选文件夹"旧流程文案、`src/stores/task.ts:8`（注释引用将删除的 `core/tasks.py` LLM_STREAM_CAP）、`test_bgm.py:24-26`（docstring 称"REAL TaskManager"，迁移时改写） | 逐条与现行代码比对 | 误导维护者 | **批次 0**（随删除项一并修正） | P3 | 高 |
| Q20 | 性能 | `api/admin.py:728/799` 每请求 spawn `nvidia-smi`；`api/bgm.py` /mix 提交前逐章同步 ffprobe | 逐行核对 | Admin 页 15s 自动刷新下重复子进程；mix 预检阻塞提交 | **批次 6**：采样缓存 + 预检异步化 | P3 | 高 |
| A-1 | 产品决策 | m4b 半分支：前端**无勾选项**（`Merge.vue:249` 固定传 `false`），后端参数面 `src/api/tts.ts:115-120` 保留，引擎恒降级 MP3（`engines/merge.py:220-221`） | 两端 + 引擎核对 | 无 UI 触达，属残留参数清理，非用户可感缺陷 | **批次 4 删除半分支**（裁决 A6）：FE 参数 + `api/tts.py:878/909/914` + `merge.py:188/220` | P3（已裁决，残留清理） | 高 |
| A-2 | 产品决策 | `src/types.ts:426` TaskControl 含 `'pause'\|'resume'`，后端 400（`api/tasks.py` control_task 仅 cancel/retry），无视图调用 | 前后端契约比对 | 类型声明不存在的能力 | **批次 4** 从类型移除 + 删 `ScriptParse.vue:82-84`"已暂停"分支（无持久化写入方）（裁决 A7） | P2（已裁决） | 高 |

---

## 四、死代码候选清单

### 4.1 可删除（高置信；全库引用核验为零：生产代码、测试、前端字符串、脚本、文档、.env、app.json 模板、CLI 入口、构建脚本均已覆盖）

| 位置 | 原用途 | 删除影响与验证 |
|---|---|---|
| `backend/platform/legacy_task_executor.py`（整文件，8 行） | 早期执行器残片 | 无；pytest + build:all |
| `platform/task_engine_support.py:120-121`（`legacy_engine_context`/`legacy_result_outcome`）及 `:67` `_write_outcome` | 引擎结果桥接旧出口 | 跑 task 相关测试 |
| `platform/storage.py:98` `user_workspace_root` | 旧工作区路径助手 | 无 |
| `platform/deps.py:41` `require_user` | 被 `require_legacy_access` 取代 | 无 |
| `engines/tts.py:29` `DEFAULT_MODEL`、`:264` `run_worker` 别名；`engines/voices.py:1140` `make_clones` 别名 | 历史别名 | 无 |
| `engines/script.py:878` `check_text_alignment`、`:2042` `_validate_instructs_batch` | 早期校验 | 跑 script 测试 |
| `IMPLEMENTED` 常量 ×3（`merge.py:34`、`script.py:39`、`tts_batch.py:62`、`voices.py:67` 中未被读取者） | 特性开关残留；仅 `tts.IMPLEMENTED` 被 `api/tts.py:42,49` 读取（保留，批次 2 改名 READY） | 无 |
| `core/paths.py:277-278` `get_layout`/`peek_layout`；`core/config.py:402-404` `clear_workspace`、`:383` `set_workspace_pointer` | 单用户时代 API | 无；`set_workspace_pointer` **核验后保留**（20+ 测试在用，为受管工作区指针的现行 setter） |
| `core/config.py:63` `tts.enabled`、`:68` `tts.speaker` | 0 读取配置字段 | 更新 config 测试；旧 app.json 中残留键由 pydantic extra=ignore 安全忽略 |
| `engines/book.py:878` `build_zip`（+ `tests/test_book.py:297`）；`engines/merge.py:337-338` `run` 别名（+ `test_merge.py:325`、`bgm.py:1520` 过期注释） | 旧入口/别名 | 连带删测试 |
| `engines/audio.py:42-43` `TOLERANCE_MIN/MAX`；`:72/89/223` `format_bytes`/`format_duration`/`output_name`（测试引用）；`book.py:1723` 局部 `import re` | 残留 | 更新测试；`output_name` **核验后保留**（`engines/audio.py:367` 生产在用） |
| `music.find_tag_category`（music.py:133，测试引用）；`legacy_tasks.py:26-28` `estimate_legacy_units`（恒 0，随 Q7 批次 4） | 残留 | 更新测试；`estimate_legacy_units` **已删**（收口 f0d05f8：请求面与估计函数彻底删除，幂等哈希键固定为常量 0 + 回放回归） |
| app.json 模板 6 个已退役键（`tts.parallel_workers/api_base/api_key/voice/concurrency`、`persona_prompts.advanced_prompt`）+ `test_config.py:48-63` 钉扎 | 已退役配置 | 更新测试断言 |
| `tts_worker.py:861` 孤儿 `@contextlib.contextmanager`（benchmark 残片） | 清理残骸 | TTS 冒烟 |
| `tts-engine/__pycache__/benchmark*.pyc` | 已删 benchmark 的字节码 | 无 |
| 零引用端点 7 个：`POST /api/auth/password`（`auth.py:112`）、`GET /api/v1/projects/{id}`（`projects.py:122`）、`POST /api/v1/tasks/{id}/retry`（`platform_tasks.py:112`）、`GET /api/tasks/{id}/stream`（`tasks.py:328`）、`POST /api/book/analyze`（`book.py:36`）、`POST /api/book/smart-split`（`book.py:93`）、`GET /api/v1/admin/users/{id}/quota`（`admin.py:378`） | 旧 UI/旧集成入口 | 无（前端走等价路径）；typecheck + 全量回归 |
| `book.analyze_text:848`/`smart_repair:1101` 的 `on_progress` 参数（生产从不同时传） | 残留参数 | 签名简化 + 测试更新 |
| `backend/core/tasks.py`（611 行）+ 其 2 个专用测试（`tests/test_tasks.py`、`tests/test_task_bus.py`） | 内存 TaskManager（裁决 A2：无外部调用方） | 无；pytest。另 6 个借用它的测试文件**迁移**而非删除（引擎 e2e 覆盖须保留），见批次 0 细则 |
| `src/components/admin/`（空目录，git 不跟踪）；`src/types.ts:754` 悬挂文档注释 | 清理残骸 | 无 |
| 3 处前端本地 `formatBytes`/`bytes`（`Usage.vue:35`、`MyResources.vue:59`、`Admin.vue:80`） | 与 `format.ts:15` **不等价**（空值/零值/小数规则不同），不可按纯删除处理 | 移出批次 0；归 Q11 批次 2 参数化统一（行为保真前提下） |
| `.env.example` 的 `POSTGRES_PASSWORD` + README 变量表对应行（裁决 A8：代码 0 读取） | 无代码读取 | README 改注"postgres 管理员密码仅供人工 psql 运维，应用不读取" |

### 4.2 随退役路线删除（已裁决，归入对应批次）

| 位置 | 裁决依据 | 批次 |
|---|---|---|
| `POST /api/text/format`（`text.py:30`）、`POST /api/book/split`（`book.py:64`）、`POST /api/script/generate-files` 非 durable 别名（`script.py:60`）、`GET /api/tasks/{task_id}`（`tasks.py:298`）、admin `/workers` `/queue` `/task-activity`、projects upload/download（`projects.py:168/195`，前端用 `/api/files` 等价路径） | 无外部访问（A3）；仅测试引用；前端走 v1/-durable 等价路径 | 0（纯测试引用者）/ 5a（随任务表面退役者） |
| `tts_worker.py` custom/design/clone 三模式（约 350 行 + CLI choices L2397-2398 收窄 + `--model` CustomVoice 默认值 L2414 核对） | A5：无手动 CLI 用法 | 4 |
| `QuotaReservation` 模型 + **迁移 0015 只删 `quota_reservations` 表**（`reserved_units`/`frozen_units` 账户列与 `ck_quota_nonnegative` 约束**保留**——前者由现行 QuotaHold 计费实写，后者 0 写点但属同一账本展示面/约束）+ `task_state.py:49-120` + 7 处空转调用 + 重试门禁 2 处 + `TaskSubmit.estimated_units`（**已按裁决彻底删除**，收口 f0d05f8：请求面字段 + `estimate_legacy_units` 删除，幂等哈希中该键固定为常量 0 保住历史回放 + 回放回归测试）+ `test_postgres_cancellation_concurrency.py:81` 重写 | A1 | 4 |
| `core/concurrency.py:85-88` `set_concurrency`、`:99-101` `set_merge_concurrency`（`ConcurrencyGate` 类与 `gate()`/`merge_gate()` 保留，恒 limit=1，文档改写） | A4 | 4 |
| m4b 半分支：`src/api/tts.ts:115-120`、UI 调用点、`api/tts.py:878/909/914`、`engines/merge.py:188/220` | A6 | 4 |
| `src/types.ts:426` `'pause'\|'resume'` + `ScriptParse.vue:82-84` 已暂停分支 + `"paused"` 状态值（无持久化写入方） | A7 | 4 |
| legacy 任务表面：`api/tasks.py` 整文件（适配层）、`-durable` 混合端点（`script.py:70/133`、`music.py:729-749`）改走 v1 通用提交、`main.py` 对应注册 | A3（5a） | 5 |

### 4.3 应保留（曾疑似、核验后确认有效）

| 位置 | 理由 |
|---|---|
| 15 个 Alembic 迁移（0001–0015） | 链完整无 no-op；0008/0013 数据迁移被 `test_migrations.py` 证明承力；0015 的 upgrade/downgrade 往返被真实 PostgreSQL 回归证明（收口 0e4bac8）；head 与 models 一致 |
| `core/concurrency.py` 的 `ConcurrencyGate` 类与 `gate()`/`merge_gate()` | 机制在用（LLM/merge 串行限流），仅 resize 表面退役 |
| `engines/tts_manifest.py` 定位 | 跨引擎产物清单职责清晰；问题是私有符号外泄（批次 3），不是模块本身 |
| `book.py`/`text.py` 职责划分 | `text.py:86-90` 文档化的刻意边界 |
| `task_control.py`（`TaskCancelled`） | 两个任务运行时共用的正确共享层 |
| `pathio.py`、`file_lock.py`、`observability.py` | 全部在用；file_lock 是跨进程共享 JSON 的正确保护 |
| 全部 15 个前端 api 文件 98 个导出（含 `ApiError` 抛出契约） | 逐一核验每个导出 ≥1 调用方 |
| `stores/pipelineState.ts` | 已验证与 task store 无双写：仅 `activeScript`/`mergeResult` 两个交接字段 |
| `taskLabels.ts` 19/19 映射 | 与 `task_types.py` 全量对齐无落空；前缀知识三份合并归批次 2 |
| `--same-same-ms` 等 CLI 参数、`TaskSnapshot`/`DurableTask`/`AdminTask` 三名词、script 侧 `-durable` 后缀 | 已验证名实相符（-durable 端点确实提交持久化任务） |
| `scripts/build-all.mjs`、`test-state-isolation.mjs`、start/stop 脚本 | 有效；bat 为 UAC 薄封装无逻辑重复 |

---

## 五、代码组织方案

**原则**：不强行套架构模式；目标 = 依赖方向单一 + 单一事实源 + 每概念一处实现；按迁移成本排序；不为审计便利引入元数据框架。

### 5.1 现状问题 → 目标结构映射

| # | 现状（证据） | 目标 | 批次 | 迁移成本 |
|---|---|---|---|---|
| S1 | 任务类型→引擎分发映射分散在 2 个分发器（`task_worker.py:657-780`、`engine_task_executor.py:28-251`）+ 3 个 frozenset（`task_types.py`） | 统一注册表：每任务类型一条声明（类型名、执行器、计费、权限），两个分发器查表；19 种显式表（不用装饰器隐式注册） | 3 | 中；影子双跑断言一致一个版本周期后删旧路径 |
| S2 | 重试/取消门禁 2 份；归属校验 4 份；`ACTIVE_TASK_STATUSES` 4 份；模块标签映射 2 份（Q6） | `services/task_operations.py` 单入口（admin 复用）+ 状态集合/标签映射单函数 | 2 | 中；行为不变，全量回归 |
| S3 | 单向反向边（无循环）：`platform/legacy_tasks.py:14`（模块级）→ `services.tasks`；`core/config.py:241/250`（函数级）→ `platform.feature_config`（原"包级循环/eager 化即炸"断言经完整导入链核对撤销） | 固定方向 `api → services → platform → core`；两条反向边按移动/下沉修正；engines→platform 额度延迟导入保留；import-linter 固化 | 3 | 中；每步单独提交 |
| S4 | 配置三文件同域（`quota_config.py` + `registration_config.py` + `feature_config.py`，SystemConfig 键读取，写入集中在 `api/admin.py`） | 合并为 `platform/system_config.py`；**不**把 quota_config 并入 `quota.py`（账本与配置是不同语义域） | 2 | 低；纯文件合并 + import 更新 |
| S5 | 引擎私有符号跨层外泄（Q16：`tts_batch._build_segments` 被 api 调用；14 个 `tts_manifest` 私有符号；`llm_json_with_retry` 从 `script.py` 反向导入） | `build_segments` 公开化；LLM JSON 工具移入 `llm_transport`；`tts_manifest` 私有符号收窄 | 3 | 中；纯移动 + 导入更新 |
| S6 | 文件下载 2 种、上传 3 种语义（含 music 无上限） | 统一走 `platform/file_response.py` 的 Range 实现；上传统一"流式 + 上限"工具 | 1（Q3）/ 2 | 中 |
| S7 | legacy HTTP 表面与 v1 表面并存（Q9） | 5a：v1 补"单连接多任务、按用户过滤"的聚合事件端点 → 前端 6 视图逐页切换 → 删 legacy 任务表面；5b（legacy 业务路由整体收敛）经裁决暂缓 | 5 | 高（跨前后端，按页面粒度回滚） |
| S8 | 前端同形拷贝（Q11：轮询 2 份、label 派生 3 份、前缀知识 3 份） | `src/utils/taskTypes.ts` 单源（以后端 `task_types.py` 为准）；`useLabelDerivedTasks(module)` composable；`useDurableTaskWait` 增加 `track(id, onTick)` 变体 | 2 | 低-中；typecheck + 各页 F5 重挂回归 |
| S9 | `Settings.vue` 双通道 + 视图突变 store（Q10）；`Admin.vue` view→view import | store 区分 root/project 配置；admin 保存只更新 root 字段；Settings 拆用户/管理两组件；对话框统一走 `dialog.ts`（Dashboard/BGM 自写对话框改 `showConfirm`，前提 `whitespace-pre-line` 可承载多行） | 5 | 低-中 |

### 5.2 明确不做（避免过度设计）

- 不拆分微服务；不引入 Celery/RQ/Arq 等第二套队列（现有 Outbox + Streams 已验证闭环）。
- 不重排 `engines/` 为更深层级——耦合来自私有符号外泄（S5），收窄即可。
- 不为 S1 引入元数据框架/装饰器注册。
- 不为已退役的 `QuotaReservation` 补估算规则——按裁决退役而非接线。

---

## 六、确认事项裁决（已由项目负责人确认）

总裁决：**所有未接线功能全部退役**；**无外部系统访问 API，可退役（无需兼容窗口）**。

| # | 事项 | 裁决 | 落地范围（已核实边界） | 批次 |
|---|---|---|---|---|
| A1 | QuotaReservation 去留 | **退役** | `platform/models.py:197` 模型 + `task_state.py:49-120`；7 处空转调用；`api/task_submission.py:58-60`、`admin.py:611` 门禁；`TaskSubmit.estimated_units`（**裁决更新 2026-09-26：彻底删除**——请求面字段 + `estimate_legacy_units` 删除，幂等哈希中该键固定为常量 0，历史回放不破，收口提交 f0d05f8）；`test_postgres_cancellation_concurrency.py:81` 重写为按操作结算断言。**保留**：`UserQuotaAccount.reserved_units`（按操作计费 `QuotaHold` 的预留/消费/释放实写该列，`quota.py:86/132/162`，删除会破坏计费）、`frozen_units`（当前 0 写点，仅展示面 `admin.py:373`、`api/quota.py:18`，同属账本展示面与约束，本轮保留）、`ck_quota_nonnegative` 约束、按操作计费（`quota.py` consume 路径）、`QuotaTransaction` 流水、管理员调整。**DB**：迁移 0015 只删 `quota_reservations` 表（不可逆，执行前先备份数据库） | 4 |
| A2 | core/tasks.py 外部集成 | **删除**（无外部调用方） | `backend/core/tasks.py`（611 行）+ 2 个专用测试删除、6 个借用测试迁移到测试专用执行器（见批次 0 细则） | 0 |
| A3 | legacy 表面去留 | **可退役，无需兼容窗口** | **5a（列入计划）**：前端 6 视图任务跟踪切 v1 事件流（v1 补聚合事件端点）→ 删 `api/tasks.py` 适配层、`-durable` 混合端点、`generate-files`/`cancel-batch` 别名、`main.py` 对应注册；Q4 由批次 1 先行最小修复（非占用等待），5a 仍按原计划退役该表面。**5b（暂缓）**：9 个 legacy 业务路由（text/book/audio/bgm/tts/script/music/files/config）整体收敛进 v1——这些端点当前接线且前端在用，不属于"未接线功能"，收益/成本比低，待 5a 落地后另行评估 | 5a→5；5b→本轮不做 |
| A4 | 并发 limit=1 意图 | **按有意保守；退役 resize 表面** | 删 `concurrency.py:85-88`、`:99-101` 及测试调用；`ConcurrencyGate` 类保留（文档改为"恒 1，有意限流"）；修 `merge.py:242` 注释。未来若要可调并发，作为新功能加回 | 4 |
| A5 | tts 死模式 | **删除**（无手动 CLI 用法） | custom/design/clone 三模式实现（约 350 行）+ CLI choices 收窄（L2397-2398）+ `--model` 默认值核对（L2414）。前置核验：grep 脚本/文档零引用 + 真实 TTS 冒烟 | 4 |
| A6 | m4b 半分支 | **隐藏并删除**（残留参数清理，非用户可感缺陷） | `src/api/tts.ts:115-120` + UI 调用点（`Merge.vue:249` 固定传 `false` 的硬编码调用）；`api/tts.py:878/909/914`；`engines/merge.py:188/220`。前端无勾选项（全库无 m4b UI），引擎恒 MP3 | 4 |
| A7 | pause/resume | **按远期处理，移除声明** | `src/types.ts:426` 删 `'pause'\|'resume'`；`ScriptParse.vue:82-84` 已暂停分支删除；`"paused"` 状态值删除（无持久化写入方）——**收口补充**：后端派生集合中的 `"paused"` 死值（`task_lifecycle.py` ACTIVE 集合、admin 两处计数）已清理（收口提交 d0c4bc2）；`models.py` 的 DB CHECK 约束仍含 `'paused'`（删除需迁移，超出本轮范围，保留）；后端 400 文案已明确（"持久化任务当前只支持取消或重试"），不动。暂停能力未来按 plan.md 早期 §7.2 要求作为独立功能重新设计 | 4 |
| A8 | POSTGRES_PASSWORD | **从模板退役** | `.env.example` 删除变量；README 变量表删行，改注"postgres 管理员密码仅供人工 psql 运维使用，应用不读取、无需存入 .env" | 0 |

**连带升级**：原"需进一步验证"清单整体转为可删除（无外部访问前提已满足）——`/api/text/format`、`/api/book/split`、script 非 durable 别名、`GET /api/tasks/{id}`、admin `/workers /queue /task-activity`、projects upload/download、`audio.py:72/89/223`、`music.find_tag_category`、`set_workspace_pointer`、`tts.enabled/tts.speaker`、6 个退役 app.json 模板键（含 `test_config.py:48-63` 更新）、`"paused"` 状态值。端点类随批次 0/5a，符号/配置类随批次 0/4。

---

## 七、命名调整清单

| # | 旧名 | 新名 | 位置 | 理由 | 受影响调用方 | 批次 |
|---|---|---|---|---|---|---|
| R1 | `platform/legacy_tasks.py` | `engine_task_submission.py` | 模块 + 12 处 import | 现名暗示"待删"，实为 13 种 legacy 引擎任务的现行提交边界 | 2 个 api 模块 + worker 侧 2 处 | 2 |
| R2 | `platform/legacy_files.py` | `file_catalog.py` | 模块 + 5 处 import | 现行为受管文件目录，非遗留 | 同上 | 2 |
| R3 | `EngineTaskContext` | `EngineExecutionContext`（或并入 TaskContext 命名族） | `platform/task_context.py:35`（对照 `:311` `PersistentTaskHandle`） | 同一任务上下文两种叫法 | 引擎侧构造点 | 2 |
| R4 | `api/task_submission.py` | `api/task_operations.py` | 模块 | 内含 cancel/retry，不止 submission | 6 处 import | 2 |
| R5 | `api/tasks.py` | —（5a 整文件删除，无需改名） | — | 若 5a 延期则临时改 `legacy_task_views.py` | 仅 main.py 注册 | 2/5 |
| R6 | `platform/task_state.py` | `platform/task_lifecycle.py` | 模块 | 含额度结算，超出"state"语义 | 7+ 处 import | 2 |
| R7 | `tts_batch._build_segments` | `tts_batch.build_segments`（公开契约） | `engines/tts_batch.py` | 被 api/tts.py 3 处、api/bgm.py 2 处跨层调用 | 5 处调用 | 2（公开化）→ 3（收窄剩余私有符号） |
| R8 | `engines/merge.py` `run` 别名 | 删除 | — | 无调用方 | 1 测试 + 1 过期注释 | 0 |
| R9 | `_empty_track_tags` | `empty_track_tags` | `engines/music.py` | 被 `api/music.py:259` 跨层调用却用私有名 | 1 处 | 2 |
| R10 | `tts.IMPLEMENTED` | `tts.READY` | `engines/tts.py` | "IMPLEMENTED" 语义不明，实为"该能力可用"开关 | `api/tts.py:42,49` | 2 |
| R11 | 前端 `workspace`（指向 project store 的变量 ×3） | `project` | `MainLayout.vue:8`、`MyResources.vue:20`、`useDurableTaskWait.ts:12` | 同名异物（workspace 概念已随项目制退役） | 局部变量 | 2 |
| R12 | `Merge.vue:42` 的 `project`（实为 pipelineState store） | `pipeline` | 局部变量 | 与真 project store 撞名 | 局部 | 2 |
| R13 | `useDurableTaskWait` 文档与 `ProjectGateAlert.vue` 文案 | 更新为"managed project 绑定 working_dir"现机制 | 3 处文案 | 描述已不存在的"选文件夹"流程 | 无（文案） | 2 |
| R14 | `music.py:730` `suggest_tags_durable` 函数名 vs 端点 `/suggest-tags`（无后缀） | 端点与函数对齐一种叫法 | `api/music.py` | 与 script 侧 `-durable` 后缀约定相反 | 前端 `music.ts` 1 处字符串 | 2（5a 时随 -durable 收敛一并处理） |
| 保留 | `--same-same-ms`、`TaskSnapshot`/`DurableTask`/`AdminTask`、script 侧 `-durable` 后缀（至 5a） | — | — | 名实相符 | — | — |

> R1–R6 均为"名字与现行职责不符"，不借改名掩盖设计问题；S1–S6 的职责调整是结构性改动，与改名解耦、可独立先行。

---

## 八、分批实施计划

每批独立可回滚（一批一提交范围，回滚 = revert）；前置条件：上一批验收通过 + 工作区干净。验收基线命令：`pytest`（全量）、`npm run typecheck`、`npm run build`、`npm run build:all`（前端相关批次）。

### 批次 0：纯删除与修复（风险最低，先行）

- **范围**：4.1 全部（含 4.2 中归批次 0 的端点/符号/配置项）+ A2（core/tasks.py 退役，细则见下）+ A8（.env.example/README）+ Q1（tts_worker 日志修复 + 失败注入回归测试，细则见下）+ Q19（过期文档/注释修正）。
- **前置**：无（全部零引用或有等价路径）。
- **验收**：全量 pytest 绿（被删项的测试同步删/改）；typecheck + build + build:all；grep 复核被删符号零残留；Q1 回归测试通过。
- **回滚**：revert；无数据影响。
- **不做**：任何改名、任何端点行为变化。

**A2 细则（core/tasks.py 退役，侦察后修正的删除/迁移拆分）**——原计划"8 个测试文件全部删除"有误：其中 6 个把 TaskManager 当作**真实线程 e2e 执行器**使用（bgm/music/script 引擎函数走真线程 + 真并发门），全删会摧毁引擎测试覆盖。正确拆分为：

1. 新建 `backend/tests/task_support.py`（测试专用线程执行器，镜像被删模块的公开表面，去掉无测试触达的 SSE 总线 / 工作区绑定 / pause / retry）：
   - `TaskStatus`（str-Enum，值与旧模块一致；**不含 `PAUSED`**，暂停能力随 A7 退役且无测试触达）与 `TERMINAL`；
   - `Task`：`id/module/label/seq/phase/status/progress/current/logs/result/error/created/finished/cancel_event`（`logs` 为 `deque(maxlen=1000)`，条目 `{"level","msg","t"}`）；
   - `TaskHandle`：`cancelled/progress/phase/log/llm_chunk/llm_rate/llm_chars/segment_stats/check`（`check()` 在取消时抛 `core.task_control.TaskCancelled`；LLM 显示类方法置空即可——测试不断言其状态）；
   - `TaskManager`：`create(module, label, func, *args, start=True, **kwargs)`（默认起守护线程，`_run` 捕获 `TaskCancelled`→CANCELLED / `Exception`→FAILED 并记"任务开始/完成/已取消/失败"日志行）、`start(task_id)`（PENDING shell）、`get/list`、`control(id, "cancel")`（未知 id 抛 `KeyError`，非 cancel 动作抛 `ValueError`）；
   - `get_task_manager()` 单例；自 `core.task_control` 再导出 `TaskCancelled`。
2. 新建 `backend/tests/__init__.py`（空包标记，使 `backend.tests.task_support` 可导入；`backend/` 本身已是包，pytest 以 `backend.tests.test_*` 命名收集，与现有 `backend.*` 绝对导入不冲突）。
3. 仅换导入行（各 1 行，不改断言）：`test_bgm.py:45`、`test_music.py:42`、`test_script.py:26` → `from backend.tests.task_support import …`（另改 `test_bgm.py:24` docstring）；`test_tts_batch.py:768`（函数内 `import backend.core.tasks as core_tasks`，仅 2 处用其 `TaskCancelled`）→ `from backend.core.task_control import TaskCancelled`；`test_voices.py:38` → 同左（其 `TaskStatus` 导入无使用，一并去掉）。
4. 纯数学窗口测试迁移：`platform/task_context.py:70-83` 的 10 秒速率窗逻辑是生产幸存路径，将其窗口计算抽为模块级纯函数 `_cps10(samples, total, now)`（行为逐字节一致：空→0；span ≤ 0.001→0；否则 `max(0.0, (total − first_total)/span)`），`EngineTaskContext.llm_rate` 改调用它；`tests/test_llm_rate_window.py` 重写为测 `_cps10`（空窗、单样本、零 span 除零防护、50/500 字每秒整窗、年轻任务部分窗 4 例）。
5. 删除 `backend/core/tasks.py`、`backend/tests/test_tasks.py`、`backend/tests/test_task_bus.py`（后两者专测被删模块的 SSE 总线与生命周期，无迁移价值）。

**Q1 细则（tts_worker 多角色子批失败路径）**：

1. 修 `tts_worker.py:1833-1834`：`log("警告：多角色子批失败：临时并发 {previous} → {temporary_concurrency}，保留未完成段继续重试")` 单参调用（沿用文件内"警告："前缀惯例，见 :1669/:2386）。
2. 为可测性做行为保持的小提升：把 `_run_batch` 内的嵌套 `run_planned_group`（:1793-1843，含 `while pending` 重试循环）提为模块级函数，`_synth_sub_batch`/`_clear_gpu_cache`/`count_batch_speakers`/`log` 改为参数注入；调用点（:1851-1876 的循环体）传原闭包，行为不变。
3. 回归测试（backend/tests 内新文件，tts_worker 顶层仅标准库导入——torch/pydub 均为函数内延迟导入，可在后端 venv 直接 import）：注入"首次子批必失败、之后成功"的 synth 与假 planner，断言：失败后日志含"警告：多角色子批失败"行（即不再抛 TypeError）、临时并发减半、`pending` 重组（`half_rows + next_pending`）后重试、恢复后后续子批正常完成且返回成功批数。

**实施经验（已在侦察中实际跑通一遍再回滚，供执行时参考）**：批次 0 的 A2 改动曾完整落地并全量验证——`pytest` **940 passed, 3 skipped（约 64 s）**、语法检查通过——后按指示回滚；上述细则即那次落地并验证过的实现。执行环境注意：全量 pytest 约 1 分钟，**前台运行并核对真实退出码**（本环境中后台 shell 会丢缓冲输出、且 `| tail` 会掩盖 pytest 退出码）；前端三件套 `npm run typecheck` / `npm run build` / `npm run build:all` 在触碰 `src/` 后运行（批次 0 仅触碰 `src/stores/task.ts` 的过期注释，`formatBytes` 三视图已移出批次 0）。

### 批次 1：正确性与健壮性修复

- **范围**：Q2（BGM 分析文件的**跨进程**读-改-发布保护：worker 侧 `bgm_storage` 与 API 侧 `api/music.py:_propagate_chapter_analysis` 均套用 `engines/music.py:455` 的 `RLock` + `exclusive_file_lock` 模式，覆盖完整 load→修改→发布 + 原子发布）、Q3（music 上传流式落盘 + 大小上限，复用 files.py 模式）、Q4（SSE 生成器内 0.5s 轮询睡眠改非占用等待：生成器协程化 + `await asyncio.sleep`，循环逻辑/认证缓存/端点表面不变；5a 仍退役该表面）。
- **前置**：批次 0。
- **验收**：新增**跨进程**并发测试（API 侧改标签 ∥ worker 侧 BGM 写分处独立进程，断言无丢失写、后写不覆盖新数据）；超大 stub 上传断言上限生效；SSE 多连接下线程池 worker 占用不再随连接数线性增长；全量回归。
- **回滚**：revert；Q2 只改写入路径，无数据迁移。

### 批次 2：单一事实源收敛（无行为变化重构 + 改名）

- **范围**：Q5（tts_manifest 两处内联清洗收敛为模块内单函数，**不**与 `safe_display_name` 合并）/Q6（状态集合、归属校验、标签映射、重试门禁收敛，S2）+ Q11 前端同形拷贝合并（S8，含 `formatBytes` 4 副本：仅按调用点语义参数化统一、显示值逐点保真，无法保真则保留现状）+ S4（system_config 合并）+ R1–R4、R6–R7、R9–R13 改名。
- **前置**：批次 1。
- **验收**：行为快照测试（收敛前后同输入同输出）；全量回归；import-linter 规则就位。
- **回滚**：按"收敛项/改名项"粒度 revert（每项一提交）。

### 批次 3：任务注册表与越层收敛

- **范围**：S1（19 种类型统一注册表，两分发器查表，影子双跑一个版本周期）+ S5（`tts_manifest` 私有符号收窄、LLM 传输双实现收敛，含 Q15：共同业务参数收敛，`stream`/`stream_options` 保留，extra_body 不对称按裁决处理）+ S3 反向边修正与 import-linter 固化。
- **前置**：批次 2。
- **验收**：19 种类型逐一"提交→worker 执行→结算"冒烟（真实 PG + Redis）；LLM 两路径**共同业务参数** diff 为空的比对测试（`stream`/`stream_options` 豁免，`extra_body` 处理按裁决后的目标行为断言）；`test_postgres_cancellation_concurrency.py` 全绿。
- **回滚**：查表改动以双跑兜底；其余 revert。

### 批次 4：休眠子系统退役（裁决落地）

- **范围**：A1（QuotaReservation 退役：代码 7 处空转调用 + 模型 + 2 处重试门禁 + **迁移 0015 只删 `quota_reservations` 表**——`reserved_units`/`frozen_units` 账户列与约束保留，执行前先备份数据库——+ 测试重写）、A4（concurrency resize 表面）、A5（tts 三死模式，前置 grep 零引用 + TTS 冒烟）、A6（m4b 残留参数）、A7（pause/resume 声明与 paused 状态值）。
- **前置**：批次 3；A5 前置核验通过。
- **验收**：全量回归；对账脚本可跑（按操作计费流水完整）；TTS 冒烟（batch/design-batch/merge 三模式）；迁移 0015 在备份后执行并在测试库先验证。
- **回滚**：代码 revert；迁移 0015 为不可逆——回滚依赖备份（执行前已生成）。

### 批次 5（5a）：legacy 任务表面退役

- **范围**：v1 补"单连接多任务、按用户过滤"的聚合事件端点 → 前端 6 视图逐页切换（ScriptParse → Voices/BatchTTS/Merge/BGM → MusicLibrary）→ 删 `api/tasks.py` 适配层、`-durable` 混合端点、`generate-files`/`cancel-batch` 非后缀别名、4.2 中归 5a 的端点 → S9（Settings 双通道修复 Q10、对话框统一 Q21 前半）。
- **前置**：批次 4。
- **验收**：每页切换后跑"F5 重挂 + 项目切换 + 取消/重试"三件套回归；全量回归。
- **回滚**：按页面粒度回滚（每页一提交）；v1 端点只增不删。

**实施与计划的偏差记录（910cf50/1c0355f/2381f24，2026-09-26 裁决记录）**：

1. 本节的"含 `stores/task.ts` 与 `tasks.ts` 退役"**与实际不符，以实际为准**：`src/stores/task.ts`、`src/api/tasks.ts` **文件保留**——5a-2 后二者是 v1 表面的前端封装层（legacy `/api/tasks` 调用方已清零）；实际退役的是 `backend/api/tasks.py` 适配层（5a-3 删文件）与 `-durable` 混合端点（5a-3 清命名）。
2. "按页面粒度回滚（每页一提交）"**退化为三提交**（5a-1 聚合端点 + 共享任务视图模块 / 5a-2 前端 6 视图切换 / 5a-3 后端表面退役 + 命名清理）：逐页切换是 5a-2 内的纯接线改动，独立页提交无额外回滚价值，裁决接受三提交粒度。
3. **回滚顺序约束**：revert 5a-2 前必须先 revert 5a-3——单独 revert 5a-2 会把前端切回 legacy `/api/tasks` 表面，而 5a-3 未同时 revert 时该表面已删，整面 404；"先 revert 5a-3"的中间态功能完整（FE 已在 v1 表面，即当前状态）。
4. `persistentTasks.ts` 的 500ms 轮询（AudioSplit/Dashboard/ProjectOverview 仍用）本轮**不动**，属后续范围。

### 批次 6：性能与收尾（可选）

- **范围**：Q20（GPU 信息采样缓存、mix 预检异步化）+ Q21 剩余（MyResources 样式统一 Tailwind）+ S9 剩余（Settings 组件拆分）。
- **前置**：批次 5。
- **验收**：admin 15s 刷新下无 nvidia-smi 重复 spawn；mix 提交延迟下降；全量回归。

**不在本轮**：5b（9 个 legacy 业务路由收敛进 v1）——经裁决暂缓，待 5a 落地后另行评估。

---

## 九、持续生效的约束（自早期迁移规格保留）

1. 优先级：数据安全与用户隔离 > 任务和额度一致性 > 现有业务兼容 > 可部署性 > 性能与开发便利。
2. 按可回滚的小批次实施；普通实现细节自行判断，不逐个文件请求确认。
3. 需要暂停并提出问题的情形：改变计费/数据归属/兼容承诺的产品规则；破坏性迁移、删除用户数据；无法在授权范围内解决的外部依赖；显著改变已确认架构或范围。提问时给出推荐方案、备选方案与影响，并继续推进不依赖该答案的工作。
4. 不可逆数据库变更（本方案仅批次 4 的迁移 0015）执行前必须备份。
5. API 不执行长时间任务，不以进程内线程承担持久任务；核心业务状态不得仅存在于内存、Redis 或 JSON 文件；Redis 丢失后能依数据库恢复调度。
6. 测试：后端改动跑完整后端测试；涉及前端跑 typecheck + build；关键一致性测试用真实 PostgreSQL 和 Redis；Mock 只隔离昂贵的模型调用，不替代事务/锁/队列验证；未执行的测试明确写"未验证"并说明原因。
7. 不依赖 API 实例内存完成认证、任务查询或状态协调（旧约束 4）。
8. 不依赖进程内锁保护跨进程业务一致性——API 与 Worker 是独立进程，跨进程一致性由数据库约束与文件锁承担（旧约束 5；收口修复的 M2 取锁序统一与 M1 回滚指纹守卫均落实本条）。
9. 所有耗时操作必须有明确的任务入口和资源限制（旧约束 7）。

---

## 十、明确不做（逐项声明，2026-09-26 收口裁决）

- 不动 `models.py` 的 DB CHECK 约束（其含 `'paused'` 值）——删除需迁移，超出本轮；后端派生集合的死值已清（收口 d0c4bc2）。
- 不改 S1 影子双跑机制（旧分发链按 plan 保留一个版本周期）。
- 不动 `persistentTasks.ts` 的 500ms 轮询（AudioSplit/Dashboard/ProjectOverview 仍用，属后续范围）。
- `src/api/tasks.ts`/`src/stores/task.ts` 更名为 `platformTasks.*` 属可选项，默认不做（文件保留为 v1 表面封装层，见批次 5 偏差记录）。
- **P3-11**：`.bgm_storage.lock` 落在 08_bgm 目录——保持：跨进程锁必须各进程锁同一物理路径、按 workspace 归属；打包/导出流程只收指定产物、不收点文件。
- **P3-12**：`llm_transport` 的 HTTPError 类型不一致——保持现状：流式分支的 `RuntimeError` message 已内嵌 `e.code`，调用方（如 `script.py` 的 `llm_json_with_retry`）不依赖异常类型/status 字段、仅记录 `last_err`；统一错误类型属传输层改造项。
- **P3-13**：`core.config` provider 注册时序硬断言——不加：模块级注册表加时序断言会与测试执行顺序耦合；维持"先用后注册即 KeyError"的现有约定 + 既有测试。
- **P3-9**：v1 聚合流 snapshot 后逐任务取事件（N+1）——声明可接受：200 任务 cap + 0.5s 节奏 + 本机单用户场景，量级可控；出现真实压力再改批量查询。
- **P3-7**：`formatBytes` 统一后 Usage 对 NaN 渲染为"未采集"（旧实现渲染 `"NaN undefined"`，属显示 bug）——保持新行为，不写代码。
- 音乐库共享 journal（`stage_shared_file` → 库索引）的回滚守卫：机制已通用（M1 的 `guard` 字段 + `guarded_lock`），库索引自身的跨进程锁（`engines/music.py`）接线属后续范围，登记为残留风险。

---

## 十一、验收记录（2026-09-26 收口修复）

对 origin/main..HEAD 43 个未推送提交（批次 0–6 实施，至 b882e54）的全量审核留下 1 高 + 6 中 + 13 低指摘；全部由下列收口提交处置，一项一提交、独立可 revert：

| 指摘 | 收口提交 |
|---|---|
| H1：task store `refresh()` 时序（流未开先等快照，一次性重挂落空） | ee71556 |
| M2 + L6：BGM 传播路径取锁序统一 + best-effort（含锁超时不 500） | d9409da |
| M6：v1 流绑定项目后才开（消除无 scope 重帧） | 63df7c2 |
| M1：共享 BGM 缓存回滚守卫（锁内指纹比对 + 真子进程跨进程场景测试） | 07fb247 |
| M3：SSE 同步 DB 单元交线程池（线程断言 + 队列无泄漏测试） | 3f883cf |
| M5：`estimated_units` 彻底删除（哈希键固定 0 + 回放回归） | f0d05f8 |
| L1：`"paused"` 派生集合清理（DB CHECK 约束保留） | d0c4bc2 |
| M4 + L13a：GPU 采样 TTL 覆盖 15s 刷新 + single-flight + 注释修正 | e08592a |
| L7：DialogHost 背景点击关闭恢复 | d7c1eb9 |
| L8 + L7b + L11 + P3-6：±Infinity 显示、BGM 确认前重验混音就绪、测试卫生、`"\j"` raw string | c7cded1 |
| P2-5 + L12 + L9 + L4：lint-imports 门禁接入 build:all + npm 脚本；.importlinter/.gitignore/foundation_schema 修正 | 560784e |
| P2-3：0015 downgrade 真实 PG 往返回归（opt-in） | 0e4bac8 |
| P1-1 + P2-4 + P3-14 + 本文销项 | （本提交） |

**执行记录**（均核对真实退出码）：

- `pytest` 全量：无 PG 变量 **979 passed / 4 skipped**（symlink 环境项 + Redis 验收 + 两项 opt-in PG 回归）；带 `NARRIFY_TEST_POSTGRES_URL`（`narrify_pg_test`）**981 passed / 2 skipped**——含 A1 取消并发与 0015 downgrade 往返，两项 opt-in PG 回归实跑 PASSED（往返约 3 s）。
- `npm run typecheck` / `npm run build`：前端改动后全绿。
- `npm run build:all`：全绿（含新接入的 lint-imports 段）；`npm run lint:imports` 单独运行 **KEPT**（1 contract, 0 broken）。
- 收尾 grep 销项：`estimated_units` 在 `src/` 零残留；`"paused"` Python 集合零残留；`/api/tasks` 路由表面零残留（注释/docstring 中的说明性文字保留）。

**未验证项（明确写出 + 原因 + 计划）**：

- Redis 验收（批次 3 验收项）：本机 127.0.0.1:6379 为**应用实例**，无专用 loopback Redis 可 flushdb——未执行；计划：部署机补跑。
- TTS 三模式冒烟（batch/design-batch/merge）：需 TTS 推理运行时/GPU 环境，本验证环境不具备——未执行；计划：交付机按批次 3 验收清单各跑一次并记录。
- 手动点检（H1"F5 重挂/项目切换/取消-重试"三件套、DialogHost 四路径、admin 多标签观察）：需真实浏览器 + 全栈环境——未执行；对应代码路径已由单元测试/线程断言/TTL 单元测试覆盖。
