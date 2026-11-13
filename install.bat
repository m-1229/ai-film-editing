@echo off
REM ===========================================================
REM  AI Film Editor - one-click environment installer (Windows)
REM  Creates .venv, installs dependencies, checks FFmpeg.
REM  Safe to re-run: it repairs an existing environment.
REM ===========================================================
setlocal
cd /d "%~dp0"
title AI Film Editor - Installer

echo ============================================================
echo   AI Film Editor - Installer
echo ============================================================
echo.

set "PY="

REM  Already installed? Reuse the venv interpreter directly.
if exist ".venv\Scripts\python.exe" (
    set "PY=%CD%\.venv\Scripts\python.exe"
    goto :run
)

call :probe py -3.11
if not defined PY call :probe py -3.12
if not defined PY call :probe py -3.10
if not defined PY call :probe py -3.13
if defined PY goto :run

REM  The python.exe shipped as a Microsoft Store alias is only a stub that
REM  pops up the Store when run, so accept a real interpreter on PATH only.
set "REALPY="
for /f "delims=" %%P in ('where python 2^>nul') do echo %%P | findstr /i "WindowsApps" >nul || set "REALPY=%%P"
if not defined REALPY goto :no_python
call :probe python
if not defined PY goto :no_python
goto :run

:probe
REM  %* is a candidate command such as "py -3.11". Keep it only if 3.10+.
for /f "usebackq delims=" %%O in (`%* -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%O"
if not defined PY goto :eof
%* -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 set "PY="
goto :eof

:run
echo.
"%PY%" setup_env.py %*
if errorlevel 1 goto :failed

echo.
echo ============================================================
echo   Done. Double-click start.bat to launch.
echo ============================================================
echo.
pause
exit /b 0

:failed
echo.
echo ============================================================
echo   Installation failed. See the messages above.
echo   Common causes: no network, or less than 1 GB free disk.
echo ============================================================
echo.
pause
exit /b 1

:no_python
echo   Python 3.10+ was not found on this computer.
echo.
where winget >nul 2>nul
if errorlevel 1 goto :manual
echo   Python 3.11 can be installed automatically for the current user
echo   (about 25 MB download, no admin rights needed).
echo.
choice /c YN /m "   Install Python 3.11 now"
if errorlevel 2 goto :manual
echo.
echo   Installing Python 3.11 via winget, this may take a few minutes...
echo.
winget install -e --id Python.Python.3.11 --scope user --silent --accept-package-agreements --accept-source-agreements
set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PY%" goto :manual
echo.
echo   Python installed: %PY%
goto :run

:manual
echo.
echo   Please install Python 3.11 first (tick "Add python.exe to PATH"):
echo     https://www.python.org/downloads/
echo.
echo   Then double-click this file again.
echo.
start "" https://www.python.org/downloads/
pause
exit /b 1
