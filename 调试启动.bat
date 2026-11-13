@echo off
REM ===========================================================
REM  Windows launcher - debug mode (console stays open)
REM  Prefers the project venv, falls back to python on PATH.
REM  You will see the full traceback here.
REM ===========================================================
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python.exe"

where "%PY%" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python.exe not found.
    echo         Create the venv first:  python -m venv .venv
    echo         Then install deps:      .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

"%PY%" main.py
echo.
echo [Process exited] Check the messages above for errors.
pause
