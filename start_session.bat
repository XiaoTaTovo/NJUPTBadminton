@echo off
setlocal
set PYTHONUTF8=1
set "PY=D:\01_Workspace\12_Dev_Projects\GitHub_project\NJUPT_badminton_booking\.venv\Scripts\python.exe"
if not exist "%PY%" (echo Missing repository uv environment. & pause & exit /b 1)
:menu
cls
echo NJUPT Local Session - Clash 7897 upstream
 echo 1. Capture own session (temporary system proxy 8080)
 echo 2. Local credential status (no network)
 echo 3. Verify credential (read-only network request)
 echo 4. Restore system proxy after abnormal exit
 echo 0. Exit
set "choice="
set /p "choice=Select: "
if "%choice%"=="0" exit /b
if "%choice%"=="1" "%PY%" "%~dp0local_session.py" capture
if "%choice%"=="2" "%PY%" "%~dp0local_session.py" status
if "%choice%"=="3" "%PY%" "%~dp0local_session.py" verify
if "%choice%"=="4" "%PY%" "%~dp0local_session.py" restore
pause
goto menu
