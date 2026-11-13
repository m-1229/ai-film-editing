@echo off
REM ===========================================================
REM  AI Film Editor - launcher (Windows)
REM  Runs a quick environment check, then starts the app.
REM  Use "start.bat --debug" (or 调试启动.bat) to keep the
REM  console open and see the full traceback.
REM ===========================================================
setlocal
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"
set "PYW=%CD%\.venv\Scripts\pythonw.exe"

if not exist "%PY%" goto :not_installed
if not exist "%PYW%" set "PYW=%PY%"

"%PY%" setup_env.py --check
if errorlevel 1 goto :broken

if /i "%~1"=="--debug" goto :debug
if /i "%~1"=="-d" goto :debug

start "" "%PYW%" main.py
exit /b 0

:debug
"%PY%" main.py
echo.
echo [Exited] Press any key to close.
pause >nul
exit /b 0

:not_installed
echo [ERROR] The environment is not installed yet.
echo         Double-click install.bat first.
echo.
pause
exit /b 1

:broken
echo.
echo [ERROR] Environment check failed, see the messages above.
echo         Run install.bat to repair it.
echo.
pause
exit /b 1
