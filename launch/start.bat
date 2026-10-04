@echo off
setlocal
cd /d "%~dp0.."

rem Preserve startup diagnostics even when the double-click console closes.
set "NARRIFY_LAUNCH_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $logDir = Join-Path (Split-Path -Parent $env:NARRIFY_LAUNCH_DIR.TrimEnd('\')) 'logs\startup'; New-Item -ItemType Directory -Path $logDir -Force | Out-Null; $logPath = Join-Path $logDir ('start-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $PID + '.log'); Start-Transcript -Path $logPath | Out-Null; try { Write-Host ('Startup log: ' + $logPath); & (Join-Path $env:NARRIFY_LAUNCH_DIR 'start.ps1') } catch { Write-Host $_ -ForegroundColor Red; Write-Host ('Startup log: ' + $logPath); exit 1 } finally { Stop-Transcript | Out-Null }"
if errorlevel 1 (
  echo.
  echo Startup failed. Press any key to close.
  pause >nul
  exit /b 1
)

echo.
echo Startup checks completed. Press any key to close this launcher.
pause >nul
endlocal
