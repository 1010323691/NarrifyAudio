[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$AppOnly)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not $AppOnly -and -not $WhatIfPreference) {
  $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator permission is required to stop data services. Double-click launch\stop.bat and approve the Windows prompt, or use -AppOnly.'
  }
}

function Test-AppProcess {
  param($Process)
  $command = $Process.CommandLine
  if (-not $command) { return $false }
  if ($Process.Name -eq 'python.exe') {
    return $command -match ('^"?' + [regex]::Escape($python) + '"?\s+-m\s+backend\.(main|worker|worker_pool)(\s|$)')
  }
  if ($Process.Name -eq 'node.exe') {
    return $command -match ([regex]::Escape($root) + '[\\/]node_modules[\\/].*vite[\\/]bin[\\/]vite\.js(?=["\s]|$)')
  }
  if ($Process.Name -eq 'cmd.exe') {
    return $command -match ('cd\s+/d\s+"' + [regex]::Escape($root) + '"\s+&&') -and
      $command -match '(-m\s+backend\.(main|worker|worker_pool)(\s|$)|npm\.cmd\s+run\s+dev(\s|$))'
  }
  return $false
}

# Identify this checkout's services by executable and command, then include
# their child processes (Python launchers, TTS subprocesses and esbuild).
# Never stop all Python/Node processes or terminate a foreign port owner.
$processes = @(Get-CimInstance Win32_Process)
$roots = @($processes | Where-Object { Test-AppProcess $_ })
$visited = New-Object 'System.Collections.Generic.HashSet[int]'
$stopOrder = New-Object 'System.Collections.Generic.List[object]'
function Add-ProcessTree {
  param($Process)
  if (-not $visited.Add([int]$Process.ProcessId)) { return }
  foreach ($child in $processes) {
    if ($child.Name -ne 'conhost.exe' -and $child.ParentProcessId -eq $Process.ProcessId -and $child.CreationDate -ge $Process.CreationDate) {
      Add-ProcessTree $child
    }
  }
  $stopOrder.Add($Process)
}
foreach ($ownedProcess in $roots) { Add-ProcessTree $ownedProcess }

if ($stopOrder.Count -gt 0) {
  Write-Host 'Stopping NarrifyAudio application processes. Running production tasks will be interrupted.'
}
foreach ($ownedProcess in $stopOrder) {
  $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($ownedProcess.ProcessId)" -ErrorAction SilentlyContinue
  if (-not $current) { continue }
  if ($current.CreationDate -ne $ownedProcess.CreationDate -or $current.CommandLine -ne $ownedProcess.CommandLine) {
    throw "Process $($ownedProcess.ProcessId) changed identity; shutdown stopped before touching data services."
  }
  if ($PSCmdlet.ShouldProcess("$($current.Name) PID $($current.ProcessId)", 'Stop NarrifyAudio process')) {
    Stop-Process -Id $current.ProcessId -Force -ErrorAction Stop
    $remaining = Get-Process -Id $current.ProcessId -ErrorAction SilentlyContinue
    if ($remaining) { $remaining.WaitForExit(5000) | Out-Null }
  }
}

if (-not $WhatIfPreference) {
  $remainingAppProcesses = @(Get-CimInstance Win32_Process | Where-Object { Test-AppProcess $_ })
  if ($remainingAppProcesses.Count -gt 0) {
    throw 'Application processes are still running. Data services were left running; retry after closing the remaining application consoles.'
  }
  Write-Host 'NarrifyAudio application processes are stopped.'
}

if (-not $AppOnly) {
  if ($PSCmdlet.ShouldProcess('Memurai and PostgreSQL 16', 'Stop NarrifyAudio data services')) {
    & (Join-Path $PSScriptRoot 'stop-data-services.ps1')
  }
}
if (-not $WhatIfPreference) { Write-Host 'Shutdown completed.' }
