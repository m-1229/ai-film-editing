@echo off
cd /d "%~dp0"

if not exist "D:\Python311\pythonw.exe" (
    echo [ERROR] pythonw.exe not found: D:\Python311\pythonw.exe
    pause
    exit /b 1
)

start "" "D:\Python311\pythonw.exe" "main.py"
exit
