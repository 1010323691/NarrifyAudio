# 单 GPU 下 LLM / TTS 动态调度

## 1. 修改文件

新增：

- `backend/core/managed_process.py`
- `backend/platform/gpu_scheduler/{__init__,config,policy,store,admission,manager,runtime}.py`
- `backend/migrations/versions/0019_gpu_scheduler.py`
- `backend/tests/test_gpu_scheduler.py`
- `src/components/settings/GpuScheduler.vue`
- `scripts/test-gpu-scheduler.mjs`
- 本文档

修改：

- `backend/platform/{models,task_registry,task_worker}.py`、`backend/worker.py`
- `backend/engines/{llm_transport,tts}.py`、`tts-engine/tts_worker.py`
- `backend/api/admin.py`、`backend/tests/test_migrations.py`
- `src/api/admin.ts`、`src/views/Admin.vue`、`package.json`、`README.md`

## 2. 新增架构

业务任务仍存入原有 Task/TaskAttempt、Outbox 和 Redis Streams。GPU 的两个逻辑队列通过数据库视图式查询获得，无新增业务任务类型或第二套任务提交接口。

```text
Worker 的调度线程（本机唯一协调者）
  ├─ QueueMonitor：任务积压、阶段请求、最老等待、活动 GPU 调用
  ├─ SchedulingPolicy：纯策略，返回目标和原因
  ├─ GPUServiceManager：服务启停、健康检查、进程树退出确认
  └─ SchedulerState：数据库状态 + 本机跨进程锁

实际 LLM 请求 / TTS 模型子进程
  └─ GPU 准入许可：等待 → running → 释放
```

数据库新增 `gpu_scheduler_state` 与 `gpu_requests`。进程身份同时保存 PID 和创建时间，避免 PID 重用造成误判。状态与准入操作共享本机文件锁；协调者在整个生命周期持有另一个排他锁。

Worker 保留解析线程，另设 LLM、TTS 任务执行通道。任务注册表是分类的单一来源：解析、语音推理基础、BGM 段落分析和音乐标签建议属于 LLM；角色候选音频、批量合成和试听属于 TTS。BGM 匹配、合并、导出等 CPU 操作不占 GPU 许可。

许可覆盖单次模型调用，而非整个业务任务，因此将来包含多类 GPU 阶段的任务也能依次等待。未领取任务按初始需求统计，运行中的任务按实际等待阶段统计；同一侧同一任务只算一个等待任务。运行数量为实际活动调用数，多个并行 LLM 请求可来自同一任务。

## 3. 调度算法

`calculate_llm_pressure()` / `calculate_tts_pressure()` 第一版返回等待任务数量，可替换为耗时和权重计算。

1. IDLE 选择压力较大的一侧；相同则选择最老任务等待较久的一侧。
2. 对方有等待任务且当前侧清空（等待数与活动 GPU 调用数均为 0）：立即发起切换，不受最短停留、冷却、压力差或已服务标志限制。
3. 当前侧仍有等待任务或活动调用时，必须先满足 min_service_runtime（默认 300 秒，可调）；对方等待超时不能突破最短停留。满足最短停留后，若当前激活周期已准入至少一个请求，对方等待超时可以突破冷却，仍不抢占活动调用。
4. 其余情况：满足时间约束，且另一侧压力减去当前压力达到阈值才切换。
5. 两侧为空时保持服务；启用空闲关闭后，且没有活动调用，超过空闲时间进入 IDLE。

两侧同时超时时，在每侧达到最短停留后交替提供服务。已有保存的最短停留配置继续生效；若之前保存为 60 秒，应在管理员“Worker / Queue”中改为 300 秒以使用五分钟时长。切换目标确定后不受新积压打断，切换完成后重新评估。达到最大等待不会中断长任务，实际等待仍可能包括排空和模型启动时间。

## 4. 服务切换与任务安全

```text
LLM_ACTIVE / TTS_ACTIVE
  → SWITCHING_TO_LLM / SWITCHING_TO_TTS / SWITCHING_TO_IDLE
  → DRAINING：原子关闭新准入，等待活动许可全部释放
  → STOPPING：正常停止旧服务，确认完整进程树退出
  → RELEASE_WAIT：等待 gpu_release_wait
  → STARTING / HEALTH_CHECK
  → LLM_ACTIVE / TTS_ACTIVE / IDLE
```

LLM 使用前台脚本，健康检查复用 `/models` 并验证所配置模型名称。TTS 仍按任务运行一次性子进程：TTS_ACTIVE 表示 GPU 已分配给 TTS，子进程加载模型并 warmup 后输出 `[ready] tts`，管理页加载阶段显示 MODEL_LOADING。无可生成输入使用 `[noop] tts`，不会伪装为模型加载完成。

Windows 子进程先以 suspended 状态创建、加入 Job Object，再恢复运行。Job 跟踪整个进程树，启动脚本退出而其子进程仍在运行时不会被判作已释放。Job 在 Worker 异常退出时清理其拥有的进程树。

调度排空和正常停止不强杀活动 GPU 调用。停止失败、排空超时、启动重试耗尽、健康失败或发现无法确认的孤儿调用进入 ERROR，关闭新 GPU 准入，保留任务。取消、暂停和 TTS watchdog 的清理仍遵守原任务契约。

等待 GPU 不创建新任务尝试，原租约续租继续运行，并响应取消。LLM 服务失效沿用原自动暂停/恢复；TTS 异常沿用有限任务重试。自动 LLM 恢复探针在托管模式下只于 LLM_ACTIVE 探测当前平台端点，避免正常切换消耗恢复次数。

恢复请求由 Worker 处理。仍有活动或无法确认结束的 GPU 调用时拒绝继续恢复，不通过清空许可绕过安全检查。确认已排空后停止旧服务、等待释放，再回到 IDLE 重新调度。修改启停、脚本路径、模型或端点会先关闭新准入，排空后应用；调度参数直接读取平台配置。

## 5. 配置与启用

调度配置位于现有 `SystemConfig` 的 `gpu_scheduler` 键。启动路径不进入项目配置、任务快照或普通用户配置接口。

| 配置 | 默认值 |
|---|---:|
| enabled | false |
| scheduler_interval | 5 秒 |
| min_service_runtime | 300 秒（可调） |
| switch_cooldown | 30 秒 |
| queue_difference_threshold | 3 |
| max_wait_time | 300 秒 |
| shutdown_when_idle | false |
| idle_shutdown_timeout | 300 秒 |
| startup_timeout | 300 秒 |
| service_stop_timeout | 30 秒 |
| drain_timeout | 1800 秒 |
| health_check_interval | 2 秒 |
| gpu_release_wait | 3 秒 |
| startup_retry_count | 3 次总尝试 |
| llm_start_script_path | 空 |
| llm_stop_script_path | 空，可选 |

启用步骤：

1. 停止旧 API / Worker，使用现有启动流程迁移至 `0019_gpu_scheduler`。单独迁移时先配置既有 NARRIFY 环境，再执行 `.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head`。
2. 管理员在“解析与 LLM”设置本机 API 地址、模型名称及服务启动脚本绝对路径，按需配置停止脚本。
3. 停止先前独立启动的本机 LLM 服务，避免未托管进程占用同一 GPU。
4. 在“Worker / Queue”启用调度，观察“服务与性能”的实时状态。

Windows 支持 `.ps1`、`.cmd` 和 `.bat`；Linux 支持 `.sh` 启动及停止脚本。启用时校验脚本类型与当前操作系统匹配。脚本工作目录为其所在目录，启动脚本必须持续前台运行，不能自行后台脱离。停止脚本应正常关闭推理服务并退出；未配置时发送进程组退出信号，停止超时进入 ERROR。PowerShell 文件遵循本机 Windows PowerShell 执行策略；应用不会修改系统执行策略，脚本被拒绝时可配置允许执行的 `.cmd` / `.bat`。

`.cmd` / `.bat` 路径支持空格和中文，为保证安全，拒绝引号、换行及命令扩展字符。含中文内容的批处理需使用与其文件编码一致的控制台代码页。

Linux `.sh` 通过 `/bin/bash -- <绝对路径>` 执行，工作目录同样为脚本所在目录，支持空格和中文路径，无需执行权限或 shebang（统一按 Bash 语法执行）。文件使用 UTF-8 和 LF 换行。示例启动脚本：

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/llm
exec /opt/llm/bin/server --host 127.0.0.1 --port 8000
```

将示例中的程序及参数替换为实际 LLM 服务，在管理页填写例如 `/opt/llm/start-llm.sh`。使用 `exec` 保持前台运行，禁止 `nohup`、后台 `&` 或 `setsid` 脱离托管进程组。未配置停止脚本时，排空后向整个进程组发送 SIGTERM，并确认组内所有非僵尸进程退出。Linux 使用独立会话/进程组，没有 Windows Job Object 在 Worker 被强制终止时自动清理的保证；重启后残留进程会阻止调度恢复，应先由管理员结束服务。此适配不替代项目原有 Windows 安装及启动脚本。

默认关闭调度，保持既有手动 LLM 和一次性 TTS 运行方式。ERROR 状态下仅关闭 enabled 不会绕过安全检查，须先结束残留调用并请求恢复。

## 6. API 变化

| 接口 | 用途 |
|---|---|
| GET /api/v1/admin/settings/gpu-scheduler | 读取调度配置 |
| PATCH /api/v1/admin/settings/gpu-scheduler | 部分更新并校验配置 |
| GET /api/v1/admin/gpu-scheduler/status | 状态、阶段、积压、GPU 调用、压力、等待、运行时长和心跳 |
| POST /api/v1/admin/gpu-scheduler/recover | 返回 202，提交安全恢复请求 |

全部要求管理员身份；写操作还要求 CSRF，并记录审计日志。状态响应不包含服务脚本路径、进程内部信息或 LLM 凭据。原有任务接口与 SSE 协议不变。

## 7. 前端变化

- “解析与 LLM”：服务启动与停止脚本配置卡片，独立保存至管理员调度接口。
- “Worker / Queue”：调度开关、全部时间与策略参数、运行状态和恢复按钮。
- “服务与性能”：实时状态卡片，显示当前服务、两侧等待任务/活动调用、压力、最老等待、运行时长、上次切换和切换阶段。
- 每 5 秒刷新，后台标签页暂停轮询；离页清理，旧账号和旧请求响应失效。心跳过期显示最后记录状态。

## 8. 验证

新增后端测试覆盖示例压力、冷却、饥饿、空闲关闭、准入互斥、阶段去重、取消、配置变更、排空与新请求竞争、有限启动重试、停止/释放失败、孤儿进程、PID 重用、TTS ready/超时，以及管理员权限、CSRF、审计和路径隔离。Windows 轻量进程测试确认 Job 跟踪子孙进程；本机假 HTTP 推理服务用于验证前台脚本的双向完整切换，不加载 GPU 模型。

前端新增 `npm.cmd run test:gpu-scheduler`，覆盖脚本与参数独立保存、账号切换、离页、请求乱序、错误保留和恢复。浏览器预览使用模拟状态检查表单保存，并输出三种管理卡片截图。

必要验证命令：

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -n 4 --dist loadscope
npm.cmd run typecheck
npm.cmd run build
npm.cmd run build:all
npm.cmd run test:state-isolation
npm.cmd run test:script-parse-workbench
npm.cmd run test:voices-workbench
npm.cmd run test:gpu-scheduler
```

本次最终验证：完整后端套件 **1152 passed、7 skipped**；其中新增调度测试 44 项通过。TypeScript 检查、Vite 构建、build:all（Python 编译及分层门禁）通过；状态隔离 12 项、文本解析工作台 10 项、角色配音工作台 10 项、GPU 管理面板 7 项前端回归通过。管理员卡片的浏览器保存检查与截图通过。

测试环境没有 npm.cmd，前端脚本使用已提供的 Node / pnpm 执行同一 package.json 脚本；未改变依赖清单或锁文件。Windows 默认 pytest 临时目录访问被拒绝，改用新建的专用临时目录，完整套件仍固定 `-n 4 --dist loadscope`。早期在仓库目录运行时出现既有文件发布用例的瞬时 WinError 5；切换到独立系统临时目录后完整套件通过。

截图位于忽略的本机 `.narrify/gpu-preview/`，分别为 `llm-scripts.png`、`scheduler-config.png` 和 `scheduler-status.png`，使用模拟管理数据。没有执行真实模型/GPU 验收，不将假进程测试当作显存释放测量。

Linux `.sh` 适配回归：调度专项 51 passed / 1 skipped；完整后端按 `-n 4 --dist loadscope` 执行，复跑结果为 1162 passed / 5 skipped（52.40 秒）；管理面板回归 7 passed；build:all（含类型检查、生产构建、Python 编译和分层门禁）通过。首次完整运行出现既有 BGM 任务等待超时，未修改该流程，停止并行构建后完整复跑通过。当前验证主机是 Windows，WSL 未安装，Linux Bash/进程组实机用例已添加但跳过；Linux 真实 GPU 切换尚未验收。

可调最短停留策略回归：专项 66 passed / 1 skipped；完整后端固定 `-n 4 --dist loadscope` 为 1177 passed / 5 skipped（53.17 秒）；管理面板回归 7 passed（含修改停留时长并保存）；build:all 通过。覆盖双向立即让出、活动调用不算清空、可调停留边界及等待超时不能提前切走。

## 9. 已知限制

- 第一版只调度同一 Windows / Linux 主机、同一项目 checkout 下的 Worker，共享一个数据库与锁目录；不支持跨机器或多个 GPU。
- TTS 模型按任务重新加载，未增加常驻服务。TTS 正常任务中的推理 watchdog 继续由原有恢复逻辑处理。
- 显存释放采用完整进程树退出加短暂等待，没有引入 Nvidia CLI 实测；`wait_gpu_released()` 为后续探测接口。
- 未托管服务不能被接管或按名称杀死；启动端口被占用会报错。其他程序自行使用 GPU 无法由本调度器阻止。
- API / Worker 重启、异常关闭后的租约与结果发布恢复继续依赖原平台机制；长 GPU 调用不会为了达到最大等待而被抢占。

## 10. 后续优化

可在策略层增加实测平均耗时和任务权重，在释放接口加入 GPU 实测探测，并在保持现有任务、取消和结果协议的前提下设计常驻 TTS 服务。第一版以可预测的切换和任务安全为优先。
