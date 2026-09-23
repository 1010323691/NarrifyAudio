$ErrorActionPreference = 'Stop'

$services = @(
  @{ Name = 'postgresql-x64-16'; Port = 5432 },
  @{ Name = 'Memurai'; Port = 6379 }
)

function Test-TcpReady {
  param([Parameter(Mandatory = $true)][int]$Port)

  $client = [System.Net.Sockets.TcpClient]::new()
  try {
    $result = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
    return $result.AsyncWaitHandle.WaitOne(750) -and $client.Connected
  } catch {
    return $false
  } finally {
    $client.Dispose()
  }
}

foreach ($item in $services) {
  $service = Get-Service -Name $item.Name -ErrorAction SilentlyContinue
  if (-not $service) {
    throw "Windows service '$($item.Name)' was not found. Install PostgreSQL 16 and Memurai Developer first."
  }

  if ($service.Status -ne 'Running') {
    Write-Host "Starting $($item.Name) ..."
    Start-Service -Name $item.Name
  } else {
    Write-Host "$($item.Name) is already running."
  }
}

$deadline = (Get-Date).AddSeconds(30)
do {
  $notReady = @($services | Where-Object { -not (Test-TcpReady -Port $_.Port) })
  if ($notReady.Count -eq 0) { break }
  Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)

if ($notReady.Count -gt 0) {
  $ports = ($notReady | ForEach-Object { "$($_.Name):$($_.Port)" }) -join ', '
  throw "Data services did not accept local TCP connections within 30 seconds ($ports). Check Windows service status and logs."
}

Write-Host 'PostgreSQL (127.0.0.1:5432) and Memurai (127.0.0.1:6379) are ready.'
