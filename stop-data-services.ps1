$ErrorActionPreference = 'Stop'

# Stop consumers/cache first, then the database. Stop the API and Worker
# console windows before running this script to avoid interrupting active work.
$services = @('Memurai', 'postgresql-x64-16')

foreach ($name in $services) {
  $service = Get-Service -Name $name -ErrorAction SilentlyContinue
  if (-not $service) {
    Write-Warning "Windows service '$name' was not found; skipping."
    continue
  }
  if ($service.Status -eq 'Stopped') {
    Write-Host "$name is already stopped."
    continue
  }

  Write-Host "Stopping $name ..."
  Stop-Service -Name $name
  $service.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(30))
  Write-Host "$name stopped."
}

Write-Host 'PostgreSQL and Memurai are stopped.'
