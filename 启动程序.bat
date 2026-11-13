@echo off
REM ===========================================================
REM  Windows launcher - background mode (no console window)
REM  Prefers the project venv, falls back to pythonw on PATH.
REM  Any error output goes to the log file next to main.py
REM ===========================================================
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

where "%PYW%" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] pythonw.exe not found.
    echo         Create the venv first:  python -m venv .venv
    echo         Then install deps:      .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

start "" "%PYW%" "main.py"
exit
