@echo off
cd /d "%~dp0"

if not exist "D:\Python311\python.exe" (
    echo [ERROR] python.exe not found: D:\Python311\python.exe
    pause
    exit /b 1
)

"D:\Python311\python.exe" main.py
echo.
echo 服务已退出。
pause
