$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$stamp = [datetime]'2026-01-01T00:00:00'
function New-ShutdownFixture {
  param([int]$Id, [int]$Parent, [string]$Name, [string]$Command, [datetime]$Created = $stamp)
  [pscustomobject]@{ ProcessId = $Id; ParentProcessId = $Parent; Name = $Name; CommandLine = $Command; CreationDate = $Created }
}
$fixtures = @(
  (New-ShutdownFixture 101 1 'python.exe' ('"' + $repoRoot + '\.venv\Scripts\python.exe" -m backend.main')),
  (New-ShutdownFixture 102 101 'python.exe' 'TTS child process'),
  (New-ShutdownFixture 103 1 'python.exe' ('"' + $repoRoot + '\.venv\Scripts\python.exe" -m backend.worker')),
  (New-ShutdownFixture 104 1 'node.exe' ('node "' + $repoRoot + '\node_modules\.bin\..\vite\bin\vite.js" --host 127.0.0.1')),
  (New-ShutdownFixture 105 104 'esbuild.exe' 'esbuild --service'),
  (New-ShutdownFixture 201 1 'python.exe' 'C:\Other\.venv\Scripts\python.exe -m backend.main'),
  (New-ShutdownFixture 202 1 'node.exe' 'node C:\Other\node_modules\vite\bin\vite.js'),
  (New-ShutdownFixture 203 1 'python.exe' ('"' + $repoRoot + '\.venv\Scripts\python.exe" -m pytest')),
  (New-ShutdownFixture 204 101 'conhost.exe' 'console host'),
  (New-ShutdownFixture 205 101 'python.exe' 'child of an earlier process using the same PID' $stamp.AddSeconds(-1))
)
$lookups = New-Object 'System.Collections.Generic.HashSet[int]'
# These mocks make the dry run deterministic; no operating-system process is
# touched, and any unexpected mutation fails the regression immediately.
function Get-CimInstance {
  param([string]$ClassName, [string]$Filter)
  if ($Filter -match 'ProcessId = (\d+)') {
    $id = [int]$Matches[1]
    $lookups.Add($id) | Out-Null
    return $fixtures | Where-Object { $_.ProcessId -eq $id }
  }
  return $fixtures
}
function Stop-Process { throw 'Dry run attempted to stop a process.' }
function Stop-Service { throw 'Dry run attempted to stop a service.' }

& (Join-Path $repoRoot 'launch\stop.ps1') -WhatIf
$expected = @(101, 102, 103, 104, 105)
if ($lookups.Count -ne $expected.Count) { throw "Unexpected shutdown scope: $lookups" }
foreach ($id in $expected) {
  if (-not $lookups.Contains($id)) { throw "Missing owned process $id" }
}
Write-Host 'PASS: owned API/Worker/Vite and descendants selected; foreign processes, console hosts and reused-parent PIDs excluded; no mutations.'
