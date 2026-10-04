@echo off
setlocal
cd /d "%~dp0.."
set "elevateScript=%TEMP%\narrify-stop-all-elevated-%RANDOM%.vbs"

fltmc >nul 2>&1
if errorlevel 1 (
  echo Requesting Windows administrator permission...
  > "%elevateScript%" echo Set UAC = CreateObject^("Shell.Application"^)
  >> "%elevateScript%" echo UAC.ShellExecute "%~f0", "", "%~dp0", "runas", 1
  cscript.exe //nologo "%elevateScript%"
  del "%elevateScript%" >nul 2>&1
  exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
if errorlevel 1 (
  echo.
  echo Shutdown failed. Review the error above.
  pause
  exit /b 1
)

echo.
echo NarrifyAudio application and data services are stopped.
pause
endlocal
