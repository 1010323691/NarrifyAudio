# scripts/dev-all.ps1
# 一键 dev 栈（供 .claude/launch.json 的 narrify-dev 条目调用，预览面板 Play 按钮入口）：
#   1) 注入 .env（应用不读 .env，缺它后端起不来）
#   2) FastAPI（8642）：已起则复用，未起则常驻后台启动
#   3) Worker：按 pidfile 检测，未起则常驻后台启动
#   4) Vite（5173）前台运行 —— 预览面板管理的是这个进程的生命周期
# 设计要点：后端是「本机会话级服务」，独立于 Vite 生命周期——预览面板 Stop 只停 Vite，
# FastAPI/Worker 常驻，避免前端页面因预览进程被回收而 500。
# 常驻的实现是「双层 Start-Process」：实测预览面板 Stop 按进程树杀整个子树（tree kill），
# 直接启动的后端会被连坐；中间 powershell 启动子进程后立即退出，API/Worker 成为孤儿进程、
# 脱离预览进程树（2026-09-29 实测：直接启动的 Worker 被 Stop 杀死，双层的 FastAPI 存活）。
# 停止后端：.\stop-data-services.ps1（先停 API/Worker 再停数据服务）或任务管理器结束 python 进程。
# 兼容 Windows PowerShell 5.1。

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# --- .env 注入 ---
$envFile = Join-Path $Root '.env'
$singleQuote = [char]39
if (-not (Test-Path $envFile)) {
    Write-Warning "未找到 .env——数据库/Redis 凭据缺失，后端大概率起不来"
} else {
    foreach ($line in (Get-Content $envFile)) {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
        $idx = $line.IndexOf('=')
        $k = $line.Substring(0, $idx).Trim()
        $v = $line.Substring($idx + 1).Trim()
        if ($v.Length -ge 2 -and $v[0] -eq $v[$v.Length - 1] -and ($v[0] -eq '"' -or $v[0] -eq $singleQuote)) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        if ($k) { Set-Item -Path "Env:$k" -Value $v }
    }
}

# --- 端口探测 ---
function Test-PortOpen([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $port)
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# --- 数据服务探测（只警告，不阻塞）---
foreach ($p in 5432, 6379) {
    if (-not (Test-PortOpen $p)) {
        Write-Warning "端口 ${p} 未监听（PostgreSQL=5432 / Memurai=6379）——先跑 .\start-data-services.ps1，否则后端起不来"
    }
}

$python = Join-Path $Root '.venv\Scripts\python.exe'
$logDir = Join-Path $Root 'logs\dev'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# --- FastAPI（8642）：已起复用，未起常驻启动 ---
if (Test-PortOpen 8642) {
    Write-Host '[dev-all] FastAPI 已在 8642 运行，复用'
} else {
    # 双层启动：中间 powershell 立即退出，python 成孤儿进程、脱离本进程树
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-Command',
        "Start-Process -FilePath '$python' -ArgumentList @('-m','backend.main') -WorkingDirectory '$Root' -WindowStyle Hidden -RedirectStandardOutput '$logDir\api.log' -RedirectStandardError '$logDir\api.err.log'")
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
        "Start-Process -FilePath '$python' -ArgumentList @('-m','backend.worker') -WorkingDirectory '$Root' -WindowStyle Hidden -PassThru -RedirectStandardOutput '$logDir\worker.log' -RedirectStandardError '$logDir\worker.err.log' | Select-Object -ExpandProperty Id | Out-File -FilePath '$workerPidFile' -Encoding ascii")
    Write-Host "[dev-all] Worker 启动中（pid 写入 $workerPidFile），日志 logs\dev\worker.log / worker.err.log"
}

# --- 等 API 健康（最长 30s，保证点完 Play 页面即可用）---
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8642/api/health' -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) {
            Write-Host '[dev-all] FastAPI 健康检查通过'
            break
        }
    } catch { }
    Start-Sleep -Seconds 1
}

# --- Vite 前台（预览面板跟踪此进程）---
& npm.cmd run dev
