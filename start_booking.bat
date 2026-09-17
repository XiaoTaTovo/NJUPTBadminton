@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Environment missing. Run setup_environment.bat once, then reopen this file.
  pause
  exit /b 2
)
"%~dp0.venv\Scripts\python.exe" -u "%~dp0launcher.py"
if errorlevel 2 pause
