@echo off
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 (
  pause
  exit /b 1
)
rem ---------------------------------------------------------------------
rem  ADMIN build -- full control. Task panel and list editing are both on.
rem
rem  Leave HOST at 127.0.0.1. This build can start processes on this
rem  machine and wipe the index, and the page has no login at all.
rem  Hand other people web-user.bat instead.
rem ---------------------------------------------------------------------
set "HOST=127.0.0.1"
set "PORT=8080"
start "" http://127.0.0.1:%PORT%
%PY% btweb.py --db bt.db --host %HOST% --port %PORT% %*
echo.
pause
