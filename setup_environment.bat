@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
echo Installing or repairing this project's environment only. No booking will be made.
where uv >nul 2>nul
if errorlevel 1 (
  echo uv not found. Install uv first.
  pause
  exit /b 2
)
uv sync --locked
if errorlevel 1 (
  echo Setup failed. Keep this window for the error message.
  pause
  exit /b 2
)
echo Ready. Use start_booking.bat for normal startup.
pause
