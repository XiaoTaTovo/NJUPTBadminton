@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist "%~dp0.venv\Scripts\python.exe" (
  where uv >nul 2>nul
  if errorlevel 1 (echo Please install uv first. & pause & exit /b 2)
  uv sync --locked --quiet
  if errorlevel 1 (echo Environment sync failed. & pause & exit /b 2)
)
"%~dp0.venv\Scripts\python.exe" "%~dp0bootstrap.py"
if errorlevel 2 pause
