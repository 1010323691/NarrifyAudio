# launch/dev-all.ps1
# 一键 dev 栈（供 .claude/launch.json 的 narrify-dev 条目调用，预览面板 Play 按钮入口）：
#   1) 注入 .env（应用不读 .env，缺它后端起不来）
#   2) FastAPI（8642）：已起则复用，未起则常驻后台启动（pid 落 logs\dev\api.pid）
#   3) Worker：按 logs\dev\worker.pid 检测，未起则常驻后台启动
#   4) Vite（5173）前台运行 —— 预览面板管理的是这个进程的生命周期
# 设计要点：后端是「本机会话级服务」，独立于 Vite 生命周期——预览面板 Stop 只停 Vite，
# FastAPI/Worker 常驻，避免前端页面因预览进程被回收而 500。
# 常驻的实现是「双层 Start-Process」：实测预览面板 Stop 按进程树杀整个子树（tree kill），
# 直接启动的后端会被连坐；中间 powershell 启动子进程后立即退出，API/Worker 成为孤儿进程、
# 脱离预览进程树（2026-09-29 实测：直接启动的 Worker 被 Stop 杀死，双层的 FastAPI 存活）。
# 常驻粒度是「会话级」：会话内预览面板 Stop 拖不垮后端；整个会话结束时宿主会把孤儿一并回收
# （同日实测）。因此「Vite 活 + 后端死」的半死态（历史页面 500 的根因）不再出现——两者同生共死。
# 停止后端：按 logs\dev\api.pid / worker.pid 里的 pid 精确 Stop-Process（脚本本身不提供停止逻辑）。
# 注意：.\launch\stop-data-services.ps1 只停 PostgreSQL/Memurai 数据服务（且需管理员），不停 API/Worker；
# 任务管理器「结束 python 进程」会误伤你手工跑的其他 python 进程，优先按 pidfile 停。
# 兼容 Windows PowerShell 5.1。

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

# --- .env 注入 ---
$envFile = Join-Path $Root '.env'
$singleQuote = [char]39
if (-not (Test-Path $envFile)) {
    Write-Warning "未找到 .env——数据库/Redis 凭据缺失，后端大概率起不来"
} else {
    foreach ($line in (Get-Content $envFile -Encoding UTF8)) {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
        $idx = $line.IndexOf('=')
        $k = $line.Substring(0, $idx).Trim()
        if ($k.StartsWith('export ', [System.StringComparison]::OrdinalIgnoreCase)) {
            $k = $k.Substring(7).Trim()
        }
        $v = $line.Substring($idx + 1).Trim()
        $quoted = $v.Length -ge 2 -and $v[0] -eq $v[$v.Length - 1] -and ($v[0] -eq '"' -or $v[0] -eq $singleQuote)
        if ($quoted) {
            $v = $v.Substring(1, $v.Length - 2)
        } else {
            # 未加引号的值：首个「空格+#」处截断行尾注释（避免误伤值内无空格的 #）
            $cm = $v.IndexOf(' #')
            if ($cm -ge 0) { $v = $v.Substring(0, $cm).Trim() }
        }
        if ($k) { Set-Item -Path "Env:$k" -Value $v }
    }
}

# --- 端口探测（双栈）：本机 Vite 默认 bind `localhost`，Node 17+ 下实际落在 IPv6 `::1`，
#     只探 127.0.0.1 会把「Vite 正在 5173 运行」误判为空闲。
#     注意必须用显式 AddressFamily 构造 TcpClient：PS 5.1（.NET Framework）下
#     自动选地址的 Connect 遇到 '::1' 会因本机主机名枚举不到 IPv6 地址而抛 WSAEAFNAMES ---
function Test-PortOpen([int]$port) {
    $targets = @(
        @{ Ip = [System.Net.IPAddress]::Parse('127.0.0.1'); Family = [System.Net.Sockets.AddressFamily]::InterNetwork },
        @{ Ip = [System.Net.IPAddress]::Parse('::1'); Family = [System.Net.Sockets.AddressFamily]::InterNetworkV6 }
    )
    foreach ($t in $targets) {
        $client = $null
        try {
            # 构造也放进 try：无 IPv6 栈的机器上 v6 家族 socket 构造可能抛异常，不能中断脚本
            $client = New-Object System.Net.Sockets.TcpClient($t.Family)
            $client.Connect($t.Ip, $port)
            if ($client.Connected) { return $true }
        } catch { }
        finally { if ($client) { $client.Close() } }
    }
    return $false
}

# --- 数据服务探测（只警告，不阻塞）---
foreach ($p in 5432, 6379) {
    if (-not (Test-PortOpen $p)) {
        Write-Warning "端口 ${p} 未监听（PostgreSQL=5432 / Memurai=6379）——先跑 .\launch\start-data-services.ps1，否则后端起不来"
    }
}

$python = Join-Path $Root '.venv\Scripts\python.exe'
$logDir = Join-Path $Root 'logs\dev'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$apiPidFile = Join-Path $logDir 'api.pid'

# --- 启动互斥锁：防止冷启动窗口内重复 Play 双拉 FastAPI/Worker。
#     锁只覆盖「启动阶段」（探测 + 拉起 + 等健康），进入 Vite 前台前释放；
#     FileStream 句柄随进程退出由 OS 自动释放，无 stale lock。
#     被阻塞的第二个实例等锁释放后走正常流程——端口/pidfile 探测自然命中「复用」；
#     因第一个实例的 Vite 此时通常尚未 bind，先在一个 8s 墙钟预算内轮询 5173，
#     仍被占用则走下方「已占用 → 优雅退出」分支；若极端等完预算仍未 bind，双 npm 抢跑、
#     后起者按 vite strictPort 响亮报错退出（无双后端、无双 Vite）---
$lockPath = Join-Path $logDir 'dev-all.lock'
$lockStream = $null
$waitedForLock = $false
try {
    $lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
    Write-Host '[dev-all] 另一个 Play 正在启动后端，等待其完成（最长 45s）...'
    $waitedForLock = $true
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        try {
            $lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $lockStream) {
        # 超时后继续流程会与仍在启动的实例双拉后端——宁可本次 Play 失败，让用户稍后重按
        Write-Warning '启动锁等待超时（另一个 Play 仍在启动后端）——为避免双拉后端，本实例退出，请稍后重新 Play'
        exit 1
    }
}

# --- FastAPI（8642）：已起复用，未起常驻启动 ---
if (Test-PortOpen 8642) {
    Write-Host '[dev-all] FastAPI 已在 8642 运行，复用'
} else {
    # 双层启动：中间 powershell 立即退出，python 成孤儿进程、脱离本进程树；
    # 退出前用 -PassThru 把 pid 写入 api.pid（与 worker.pid 对称，供精确停止）
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-Command',
        "Start-Process -FilePath '$python' -ArgumentList @('-m','backend.main') -WorkingDirectory '$Root' -WindowStyle Hidden -PassThru -RedirectStandardOutput '$logDir\api.log' -RedirectStandardError '$logDir\api.err.log' | Select-Object -ExpandProperty Id | Out-File -FilePath '$apiPidFile' -Encoding ascii")
    Write-Host '[dev-all] FastAPI 启动中（8642），日志 logs\dev\api.log / api.err.log'
}

# --- Worker：按 pidfile 检测（非管理员下 CIM CommandLine 常读空，命令行检测不可靠；
#     Get-Process -Id 不需要读命令行。pid 被系统复用给别的 python 进程时会误判为在跑，
#     概率低且后果仅是「没自动拉起新 Worker」，可手动停掉后端重开）---
$workerPidFile = Join-Path $logDir 'worker.pid'
$workerRunning = $false
if (Test-Path $workerPidFile) {
    try {
        $oldPid = [int]((Get-Content $workerPidFile -Raw).Trim())
        $p = Get-Process -Id $oldPid -ErrorAction Stop
        $workerRunning = ($p.ProcessName -eq 'python')
    } catch { $workerRunning = $false }
}
if ($workerRunning) {
    Write-Host '[dev-all] Worker 已在运行，复用'
} else {
    # 双层启动（同 FastAPI）：中间 powershell 退出前用 -PassThru 把 pid 写入 pidfile，
    # 之后退出，Worker 脱离本进程树
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-Command',
        "Start-Process -FilePath '$python' -ArgumentList @('-m','backend.worker_pool') -WorkingDirectory '$Root' -WindowStyle Hidden -PassThru -RedirectStandardOutput '$logDir\worker.log' -RedirectStandardError '$logDir\worker.err.log' | Select-Object -ExpandProperty Id | Out-File -FilePath '$workerPidFile' -Encoding ascii")
    # pidfile 由中间进程写入（旧文件内容会被覆盖）：等它的内容真正变化（最长 10s）。
    # 没变 = 中间层根本没跑通（如日志文件被占导致 redirect 失败），不能静默
    $oldPidContent = $null
    if (Test-Path $workerPidFile) { $oldPidContent = (Get-Content $workerPidFile -Raw).Trim() }
    $pidFileWritten = $false
    for ($i = 0; $i -lt 20; $i++) {
        $nowPidContent = $null
        if (Test-Path $workerPidFile) { $nowPidContent = (Get-Content $workerPidFile -Raw).Trim() }
        if ($nowPidContent -and $nowPidContent -ne $oldPidContent) { $pidFileWritten = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $pidFileWritten) {
        Write-Warning 'Worker 启动后 worker.pid 未更新——中间层可能失败（如日志文件被占用），查看 logs\dev\worker.err.log'
    }
    Write-Host "[dev-all] Worker 启动中（pid 写入 $workerPidFile），日志 logs\dev\worker.log / worker.err.log"
}

# --- 等 API 健康（最长 30s，保证点完 Play 页面即可用）；超时不静默 ---
$healthOk = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8642/api/health' -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) {
            Write-Host '[dev-all] FastAPI 健康检查通过'
            $healthOk = $true
            break
        }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $healthOk) {
    Write-Warning 'FastAPI 30s 内健康检查未通过（8642 可能被非 FastAPI 进程占用、或后端起失败）——查看 logs\dev\api.err.log，此时前端请求会 500/502'
}

# --- 启动阶段结束：释放互斥锁（后续 Vite 前台进程不再持锁）---
if ($lockStream) { $lockStream.Dispose() }

# --- 走过锁等待的第二个实例：第一个实例的 Vite 通常还没 bind 5173，给它一点时间完成 bind，
#     好让下面走「已占用 → 优雅退出」，而不是双 npm 抢跑。
#     用墙钟预算（8s）而非固定轮数：端口关闭时本机探测一轮含数秒连接拒绝延迟
#     （机器级特性），固定 12 轮最坏会拖到数十秒 ---
if ($waitedForLock) {
    $pollDeadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $pollDeadline) {
        if (Test-PortOpen 5173) { break }
        Start-Sleep -Milliseconds 500
    }
}

# --- Vite 前台（预览面板跟踪此进程）；5173 已被占用（前一个 Play 实例仍活着）时
#     不重复起 Vite（vite strictPort 会直接报错），优雅退出本进程 ---
if (Test-PortOpen 5173) {
    Write-Host '[dev-all] Vite 已在 5173 运行（前一个 Play 实例仍存活）——本实例不重复启动 Vite，退出'
    exit 0
}
& npm.cmd run dev
