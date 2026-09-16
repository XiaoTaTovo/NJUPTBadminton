@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (py -m venv .venv && .venv\Scripts\python.exe -m pip install -r njpt_booking\requirements.txt)
:menu
cls
echo ?????????????????
echo 1. ????
echo 2. ??????????? token?
echo 0. ??
set /p c=????
if "%c%"=="1" .venv\Scripts\python.exe -m njpt_booking.cli check --config njpt_booking\example.yaml
if "%c%"=="2" .venv\Scripts\python.exe -m njpt_booking.cli preview --config njpt_booking\example.yaml
if "%c%"=="0" exit /b
pause
goto menu
