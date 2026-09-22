$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$nodeModules = Join-Path $root 'node_modules'
$backendUrl = 'http://127.0.0.1:8642/api/health'
$frontendUrl = 'http://localhost:5173'

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

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
  throw "Missing shared Python environment: $python. Run install_tts_env.ps1 first."
}

if (-not (Test-Path -LiteralPath $nodeModules -PathType Container)) {
  throw "Missing frontend dependencies: $nodeModules. Run npm.cmd install first."
}

$backendReady = Test-HttpReady -Uri $backendUrl
if (-not $backendReady) {
  $quotedRoot = [char]34 + $root + [char]34
  $quotedPython = [char]34 + $python + [char]34
  $backendCommand = "cd /d $quotedRoot && $quotedPython -m backend.main"
  Start-Process -FilePath (Join-Path $env:WINDIR 'System32\cmd.exe') -ArgumentList @('/k', $backendCommand) -WorkingDirectory $root
  Write-Host 'Backend starting on http://127.0.0.1:8642 ...'
} else {
  Write-Host 'Backend is already running.'
}

$frontendReady = Test-HttpReady -Uri $frontendUrl
if (-not $frontendReady) {
  $quotedRoot = [char]34 + $root + [char]34
  $frontendCommand = "cd /d $quotedRoot && npm.cmd run dev"
  Start-Process -FilePath (Join-Path $env:WINDIR 'System32\cmd.exe') -ArgumentList @('/k', $frontendCommand) -WorkingDirectory $root
  Write-Host 'Frontend starting on http://localhost:5173 ...'
} else {
  Write-Host 'Frontend is already running.'
}

$backendReady = Wait-HttpReady -Uri $backendUrl
$frontendReady = Wait-HttpReady -Uri $frontendUrl

if (-not $backendReady) {
  Write-Warning 'Backend did not become ready within 30 seconds. Check its console window.'
}

if (-not $frontendReady) {
  Write-Warning 'Frontend did not become ready within 30 seconds. Check its console window.'
}

Start-Process $frontendUrl
Write-Host ''
Write-Host 'AudiobookStudio is starting.'
Write-Host 'Close the Backend and Frontend windows to stop the development servers.'
