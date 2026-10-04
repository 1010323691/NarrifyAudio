$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$nodeModules = Join-Path $root 'node_modules'
$dotenvPath = Join-Path $root '.env'
$localBin = Join-Path $root '.narrify\bin'

# SoX_ng ships the modern Windows SoX-compatible executable under the name
# sox_ng.exe. Qwen's audio helpers look for sox.exe, so provide a local alias
# inside the ignored runtime directory without changing the system installation.
$soxPackage = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\sox_ng.sox_ng_*') -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
if ($soxPackage) {
  $soxSource = Join-Path $soxPackage.FullName 'sox_ng.exe'
  if (Test-Path -LiteralPath $soxSource -PathType Leaf) {
    New-Item -ItemType Directory -Path $localBin -Force | Out-Null
    Copy-Item -LiteralPath $soxSource -Destination (Join-Path $localBin 'sox.exe') -Force
    $env:PATH = "$localBin;$($soxPackage.FullName);$env:PATH"
  }
}

# Import the local app settings from .env for the child backend process.
# PostgreSQL and Memurai run as Windows services; this file supplies app settings.
if (Test-Path -LiteralPath $dotenvPath -PathType Leaf) {
  foreach ($line in Get-Content -LiteralPath $dotenvPath -Encoding UTF8) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
    $separator = $trimmed.IndexOf('=')
    if ($separator -lt 1) { continue }
    $key = $trimmed.Substring(0, $separator).Trim()
    if ($key -notmatch '^NARRIFY_[A-Z0-9_]+$') { continue }
    $value = $trimmed.Substring($separator + 1).Trim()
    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    [System.Environment]::SetEnvironmentVariable($key, $value, 'Process')
  }
}

$backendUrl = 'http://127.0.0.1:8642/api/health'
$frontendHealthUrl = 'http://127.0.0.1:5173/'
$frontendUrl = 'http://127.0.0.1:5173/#/login'

function Test-HttpReady {
  param(
    [Parameter(Mandatory = $true)][string]$Uri,
    [int]$TimeoutSeconds = 1
  )

  try {
    $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec $TimeoutSeconds
    return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
  } catch {
    return $false
  }
}

function Wait-HttpReady {
  param(
    [Parameter(Mandatory = $true)][string]$Uri,
    [int]$TimeoutSeconds = 30
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    if (Test-HttpReady -Uri $Uri -TimeoutSeconds 2) {
      return $true
    }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)

  return $false
}

function Test-TcpReady {
  param(
    [Parameter(Mandatory = $true)][string]$HostName,
    [Parameter(Mandatory = $true)][int]$Port,
    [int]$TimeoutMilliseconds = 1000
  )

  $client = [System.Net.Sockets.TcpClient]::new()
  try {
    $result = $client.BeginConnect($HostName, $Port, $null, $null)
    return $result.AsyncWaitHandle.WaitOne($TimeoutMilliseconds) -and $client.Connected
  } catch {
    return $false
  } finally {
    $client.Dispose()
  }
}

function Ensure-WindowsService {
  param([Parameter(Mandatory = $true)][string]$Name)

  $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
  if (-not $service) {
    throw "Required Windows service '$Name' is not installed. Install the Windows-native PostgreSQL and Memurai services first."
  }
  if ($service.Status -ne 'Running') {
    Write-Host "Starting Windows service $Name ..."
    Start-Service -Name $Name
    $service = Get-Service -Name $Name
  }
  if ($service.Status -ne 'Running') {
    throw "Windows service '$Name' did not start."
  }
}

Ensure-WindowsService -Name 'postgresql-x64-16'
Ensure-WindowsService -Name 'Memurai'

if (-not (Test-TcpReady -HostName '127.0.0.1' -Port 5432)) {
  throw 'Windows PostgreSQL is not accepting connections on 127.0.0.1:5432.'
}
if (-not (Test-TcpReady -HostName '127.0.0.1' -Port 6379)) {
  throw 'Windows Memurai is not accepting connections on 127.0.0.1:6379.'
}

$backendReady = Test-HttpReady -Uri $backendUrl
$listener = Get-NetTCPConnection -State Listen -LocalPort 8642 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($backendReady) {
  if (-not $listener) {
    throw 'The API health endpoint responded but no local Windows listener owns port 8642.'
  }
  $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
  if (-not $owner -or $owner.Name -ne 'python.exe' -or $owner.CommandLine -notlike "*$python*") {
    $ownerName = if ($owner) { $owner.Name } else { 'unknown process' }
    throw "Port 8642 is responding through $ownerName, not this Windows .venv. Stop the old backend before continuing."
  }
  Write-Host 'Windows-native backend is already running.'
} else {
  if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    $ownerName = if ($owner) { $owner.Name } else { 'unknown process' }
    throw "Port 8642 is occupied by $ownerName. Stop the old backend before starting this Windows-native backend."
  }

  if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found. Run .\install_tts_env.ps1 -PythonVersion 3.14 first."
  }

  if (-not (Test-Path -LiteralPath $dotenvPath -PathType Leaf)) {
    throw 'Missing .env. Create the local settings file before starting NarrifyAudio.'
  }

  Write-Host 'Applying database migrations...'
  & $python -m alembic upgrade head
  if ($LASTEXITCODE -ne 0) {
    throw 'Database migrations failed. Confirm PostgreSQL is healthy and NARRIFY_DATABASE_URL in .env is correct.'
  }

  $quotedRoot = [char]34 + $root + [char]34
  $quotedPython = [char]34 + $python + [char]34
  $backendCommand = "cd /d $quotedRoot && $quotedPython -m backend.main"
  Start-Process -FilePath (Join-Path $env:WINDIR 'System32\cmd.exe') -ArgumentList @('/k', $backendCommand) -WorkingDirectory $root
  Write-Host 'Backend starting on http://127.0.0.1:8642 ...'
}

$workerProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*$python*" -and $_.CommandLine -like '*-m backend.worker*' }
if (-not $workerProcesses) {
  if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found. Run .\install_tts_env.ps1 -PythonVersion 3.14 first."
  }
  $quotedRoot = [char]34 + $root + [char]34
  $quotedPython = [char]34 + $python + [char]34
  $workerCommand = "cd /d $quotedRoot && $quotedPython -m backend.worker"
  Start-Process -FilePath (Join-Path $env:WINDIR 'System32\cmd.exe') -ArgumentList @('/k', $workerCommand) -WorkingDirectory $root
  Write-Host 'Windows task worker starting ...'
} else {
  Write-Host 'Windows task worker is already running.'
}

$frontendReady = Test-HttpReady -Uri $frontendHealthUrl
if (-not $frontendReady) {
  if (-not (Test-Path -LiteralPath $nodeModules -PathType Container)) {
    throw "Frontend dependencies are missing. Run npm.cmd install first."
  }

  $quotedRoot = [char]34 + $root + [char]34
  $frontendCommand = "cd /d $quotedRoot && npm.cmd run dev -- --host 127.0.0.1"
  Start-Process -FilePath (Join-Path $env:WINDIR 'System32\cmd.exe') -ArgumentList @('/k', $frontendCommand) -WorkingDirectory $root
  Write-Host 'Frontend starting on http://localhost:5173 ...'
} else {
  Write-Host 'Frontend is already running.'
}

$backendReady = Wait-HttpReady -Uri $backendUrl
$frontendReady = Wait-HttpReady -Uri $frontendHealthUrl

if (-not $backendReady) {
  Write-Warning 'Backend did not become ready within 30 seconds. Check its console window.'
}

if (-not $frontendReady) {
  Write-Warning 'Frontend did not become ready within 30 seconds. Check its console window.'
}

Write-Host ''
Write-Host 'NarrifyAudio development environment'
Write-Host "Backend:  $backendUrl"
Write-Host "Frontend: $frontendUrl"
Write-Host 'Worker:   Windows Python task worker'
Write-Host 'Admin login: http://127.0.0.1:5173/#/admin/login'
Write-Host 'Close the Backend, Worker and Frontend console windows to stop processes started by this script. PostgreSQL and Memurai remain Windows services.'

if ($backendReady -and $frontendReady) {
  Write-Host 'Opening NarrifyAudio in your browser in 5 seconds...'
  Start-Sleep -Seconds 5
  Start-Process -FilePath $frontendUrl
}
