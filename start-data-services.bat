@echo off
setlocal
cd /d "%~dp0"
set "elevateScript=%TEMP%\narrify-start-elevated-%RANDOM%.vbs"

fltmc >nul 2>&1
if errorlevel 1 (
  echo Requesting Windows administrator permission...
  > "%elevateScript%" echo Set UAC = CreateObject^("Shell.Application"^)
  >> "%elevateScript%" echo UAC.ShellExecute "%~f0", "", "%~dp0", "runas", 1
  cscript.exe //nologo "%elevateScript%"
  del "%elevateScript%" >nul 2>&1
  exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-data-services.ps1"
if errorlevel 1 (
  echo.
  echo Could not start PostgreSQL and Memurai. Review the error above.
  pause
  exit /b 1
)

echo.
echo PostgreSQL and Memurai are ready. You can now run start.bat to launch NarrifyAudio.
pause
endlocal
