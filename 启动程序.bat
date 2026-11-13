@echo off
REM 中文名入口：保留原来的使用习惯，实际逻辑在 start.bat。
REM 后台启动，不弹控制台窗口。
call "%~dp0start.bat" %*
