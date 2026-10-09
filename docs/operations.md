# 运维、配置与数据

[项目首页](../README.zh-CN.md) · [English](operations.en.md) · [Windows 部署](windows.md) · [Linux 部署](../readme-linux.md) · [制作指南](production.md) · [开发指南](development.md)

## 配置的三个层次

| 配置 | 保存位置 | 管理方式 |
| --- | --- | --- |
| 数据库、Redis、Cookie、连接池、初始管理员 | 进程环境中的 `NARRIFY_*`，本机通常保存为 `.env` | 按部署指南导入；修改后重启相关进程 |
| 注册开关、初始字符额度、LLM、制作默认参数、GPU 调度、存储根目录 | PostgreSQL 的平台设置 | 管理控制台 |
| 项目制作参数与运行资料 | 项目工作空间，包含 `config/setting.json` | 项目工作台与应用配置逻辑 |

API、Worker 和 Alembic 不会自行加载 `.env`。Windows `launch/start.ps1` 会注入其中的 `NARRIFY_*`；Linux 使用部署指南中的环境文件或启停脚本。单独运行模块时也要先导入同一套环境。

`.env.example` 是部署变量模板。`NARRIFY_STORAGE_ROOT` 默认指向仓库的 `storage/`，管理控制台保存的存储根目录优先。不要靠编辑路径或手动挪动目录替代控制台的存储迁移；迁移要求任务排空并校验冲突。

根目录 `setting.json` 是受跟踪的默认模板，提交时工作空间路径应为空，LLM key 只用 `local` 占位值。项目内的配置和旧版 `config/app.json` 是用户数据；读取优先使用 `setting.json`，保存时会生成新配置并保留旧文件。不要将真实凭据、本机路径、日志或工作空间加入版本控制。

默认开放注册，新用户初始字符额度为 0；管理员可关闭注册、设置新用户初始额度并调整账号额度。LLM 按接受的业务输出字符结算，TTS 按输入字符预留并在结果发布时结算。这是应用内部额度，不等于上游服务的 token 或费用。bootstrap 管理员只用于首次初始化，改 `.env` 中的 bootstrap 密码不会重置已存在账号。

浏览器使用 Cookie session 与 CSRF。对外部署按平台指南配置 HTTPS、`NARRIFY_COOKIE_SECURE` 和允许来源；优先采用同源反向代理。分域前端的 `VITE_API_BASE` 与 `VITE_CSRF_COOKIE_NAME` 是构建时配置，修改后重新构建，同时核对后端来源与 Cookie 策略。

## Worker 池与并发

正式入口是 `python -m backend.worker_pool`，默认监督 **4 个机械 Worker + 4 个模型 Worker**，槽位退出后按退避策略重启。机械任务包括排版、分册、资源扫描、合并和混音；模型任务包括解析、角色分析/克隆、合成、预览渲染、BGM 场景分析和音乐标签推荐。

可用 `--mechanical-workers` 和 `--model-workers` 调整进程数量，均至少为 1。直接运行 `backend.worker` 默认只领取机械任务；调试模型任务需显式 `--task-lane model`。不要把单 Worker 当成完整任务服务。

LLM 请求上限由平台 `generation.parse_worker_concurrency` 控制，默认 4，所有模型 Worker 共用，不随进程数叠加。另有全机 LLM 活跃任务上限（该配置的两倍）；任务数、进程数、线程数和同时模型请求数是不同概念。TTS 模型许可与 GPU 调度另行控制。同一项目冲突写任务会排队；不同章节的非冲突合并/混音可以并行。

数据库业务池和请求锁池独立。默认单 API 上限为 `(16+0)+(8+0)=24`，每个 Worker 为 `(2+4)+(1+0)=7`，八个 Worker 合计为 **80 个连接**。这是连接池上限预算，不是启动时固定占用；还要给迁移、管理连接和其他应用留余量。多 API 或增加 Worker 时重新计算预算，变量以 `.env.example` 为准。Worker 业务池默认等待 30 秒，API 及锁池默认等待 3 秒。已有环境中的显式配置优先，升级后需核对并重启；瞬时数据库故障会退避重试，不能代替故障排查。

## 可选：本机单 GPU 服务调度

调度默认关闭，用于本机 LLM 与 TTS 共用 GPU 时按需求切换服务。远程 LLM 可用于制作，但不受此单 GPU 调度管理。

启用前完成以下准备：

1. 验证 NVIDIA 驱动、CUDA 版 PyTorch、TTS 依赖和模型，并用短文本真实合成；验证 LLM 模型可通过配置的 API 调用。
2. 在管理控制台配置平台 LLM 地址、模型及凭据。启用调度时地址必须为 HTTP/HTTPS，主机为 `localhost`、`127.0.0.1` 或 `::1`，模型名不能为空。
3. 配置 LLM 前台启动脚本的绝对路径；停止脚本可选。支持与当前系统相符的 `.ps1`、`.cmd`、`.bat` 或 `.sh` 文件，脚本必须存在。启动脚本应使 LLM 进程生命周期可管理，避免脱离监督的后台服务。
4. 停止原来独立运行的 LLM，确认没有其他服务占用同一 GPU，再在管理员 Worker / Queue 设置中启用调度，保持模型 Worker 在线。

管理员可调整最短运行时间、切换冷却、排队差异、最长等待、启动/停止/排空超时及空闲关闭策略。先采用默认值，再结合真实显存与等待情况调整。状态进入 ERROR 时先查看事件与服务日志、修复脚本或模型问题，再使用恢复入口；恢复不是重新安装驱动或模型。

## 备份、升级与旧布局迁移

**数据库、实际存储根目录及本机配置必须一起备份**；成品 ZIP 不包含完整项目，也不能恢复数据库记录。包含自备 BGM 时同时保留实际音乐库；保留模型缓存可避免重新下载。

停止应用写入后，在仓库根目录执行备份。以下采用默认数据库名/用户名；按实际部署修改，并把存储路径替换为控制台显示的实际根目录。备份目标应放在存储根目录之外，完成后保存到独立备份介质，检查命令退出状态和文件内容。备份包含凭据与用户资料，需要限制访问。

```powershell
# Windows；psql/pg_dump 密码提示输入应用数据库的原始密码
$backupDir = Join-Path '.backups' ('narrify-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$storageRoot = 'D:\replace-with-actual-storage-root'
& "$env:ProgramFiles\PostgreSQL\16\bin\pg_dump.exe" -h 127.0.0.1 -p 5432 -U narrify -d narrify -W -Fc -f (Join-Path $backupDir 'database.dump')
if ($LASTEXITCODE -ne 0) { throw '数据库备份失败' }
Copy-Item -LiteralPath $storageRoot -Destination (Join-Path $backupDir 'storage') -Recurse -ErrorAction Stop
foreach ($item in @('.env', 'setting.json', 'app.json', 'config', 'logs', '.narrify', 'music_library')) {
  if (Test-Path -LiteralPath $item) { Copy-Item -LiteralPath $item -Destination $backupDir -Recurse -ErrorAction Stop }
}
```

```bash
# Linux；运行账号须能读取应用文件并连接数据库
umask 077
backup_dir="$HOME/narrify-backups/$(date +%Y%m%d-%H%M%S)"
storage_root='/replace-with-actual-storage-root'
mkdir -p "$backup_dir"
pg_dump -h 127.0.0.1 -p 5432 -U narrify -d narrify -W -Fc -f "$backup_dir/database.dump" || exit 1
tar -czf "$backup_dir/storage.tar.gz" -C "$storage_root" . || exit 1
runtime_paths=()
for item in .env setting.json app.json config logs .narrify music_library; do
  if [ -e "$item" ]; then runtime_paths+=("$item"); fi
done
if [ "${#runtime_paths[@]}" -gt 0 ]; then
  tar -czf "$backup_dir/runtime.tar.gz" "${runtime_paths[@]}" || exit 1
fi
```

Linux 的 systemd 环境文件若保存于仓库外（例如 `/etc/narrify-audio/`），另按其实际位置与权限备份，不能只保存仓库内 `.env`。模型缓存或外置音乐库也按实际位置单独保留。

升级前先结束或取消任务，确认没有待处理、暂停或重试任务，再停止 API、Worker 池和前端。保留数据服务以执行备份与迁移。备份 PostgreSQL（例如 `pg_dump -Fc`）、全部实际工作空间、`.env` 及本机配置，然后更新源码、用 `npm.cmd ci`（Linux 为 `npm ci`）更新前端依赖，并核对基础/TTS 依赖变化。

- **Windows：** 按 [Windows 指南](windows.md)启动；`launch/start.ps1` 在 API 启动前执行 Alembic 迁移。升级后重新登录并提交一个基础文本任务。
- **Linux：** 按 [Linux 指南](../readme-linux.md)更新构建及 systemd 配置；使用 `RemainAfterExit` 的迁移服务必须显式 restart 才会再执行迁移。改变 Worker 入口或 unit 时执行 daemon-reload 后重启服务，源码更新不会改写已安装的 unit。

从旧 UUID 工作空间布局升级时，先完成相同的备份与停机，并确保未完成任务已处理。在应用的 `NARRIFY_*` 环境下运行：

```powershell
# Windows，仓库根目录
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m backend.services.workspace_migration
```

```bash
# Linux，仓库根目录
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m backend.services.workspace_migration
```

布局迁移会按项目名称规范目录、整理旧输入结构并更新路径引用；数据库 ID 保持稳定。该命令可重复运行，存在未完成任务或工作空间正被使用时会拒绝执行。升级后检查原文、角色、音频与配置，再验收解析、合成和成品领取；不要通过删库重新初始化完成升级。

存储根目录中的 `.layout-migrations/` 保存布局移动与回滚日志，迁移或恢复尚未完成时不要删除。迁移保留未登记历史文件：无法归类的旧文件/目录转入项目 `07_output/历史文件/`，旧缓存整理到临时区域；保留文件不代表获得成品下载资格。

## 项目工作空间

托管项目使用 `存储根目录/用户名/项目名称/`。数据库 ID 保持稳定，代码通过 `Project.directory_key` 获取物理路径，不能自行拼接项目 ID。

```text
00_temp/           临时文件、任务尝试与发布日志
01_input/          上传原文
02_split_text/     排版文本与拆分章节
03_parsed_json/    书籍、剧本与音频分析
04_voice_profiles/ 角色与声音资料
05_audio_chunk/    合成片段
06_audio_merge/    合并音频
07_output/         成品与其他持久输出
08_bgm/            BGM 分析与混音资料
config/            项目配置
logs/              项目日志
```

项目名称遵循 Windows 文件夹命名限制，目录名不区分大小写。项目有未完成任务时禁止重命名；正常重命名同步调整目录、数据库与路径引用，不要直接在文件管理器中重命名。需要保留历史的同名上传/产物使用 `文件 (2).扩展名` 等数字后缀；明确更新制作模块的任务仍沿用模块更新规则。回收站项目目录使用 `项目名称（回收站 N）`；恢复尝试使用项目原名，同名冲突使用 `（恢复）` 后缀。

## 资源与故障排查

“我的资源”通过 Worker 扫描生成清单；外部修改工作空间后点击刷新，不是文件系统实时监听。扫描失败不会替换上一份完整清单；部分清单和未知容量需要处理扫描问题后重试。

仅验证过的交付成品能下载和打包；合成片段、制作资料与部分合并音频不能领取。导出包保留 7 天，来源项目进入回收站后无法下载。缓存清理只处理允许缓存/临时目录内超过 7 天且符合安全检查的普通文件，删除不可恢复；取消也不会恢复此前已删文件。项目回收站保留一个日历月，到期由 Worker 清理。

页面能打开但任务不运行时，检查数据库、Redis、两个 Worker 分工的在线状态、Outbox/队列、任务事件及额度。`/api/health` 不检查数据库或 Worker，必须登录并执行真实任务验收。解析故障检查 LLM 地址、模型、凭据与额度；TTS 故障检查隔离进程日志、驱动/torch、模型和 FFmpeg。优先查看第一个错误，避免反复提交相同失败任务。
