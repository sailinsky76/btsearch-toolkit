@echo off
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 (
  pause
  exit /b 1
)
rem ---------------------------------------------------------------------
rem  USER build -- search only.
rem
rem  --no-tasks  hides the task button and makes /api/task and /api/browse
rem              answer 403
rem  --no-delete hides the edit-list button and the per-row checkboxes,
rem              opens the database read-only, and makes /api/delete 403
rem
rem  Both flags matter. Hiding the buttons alone stops nobody who knows
rem  the URLs, so the server refuses the calls as well.
rem
rem  Want other machines on the LAN to reach it? Set HOST to 0.0.0.0.
rem  There is still no login -- anyone who can reach this box can search.
rem
rem  PORT differs from web-admin.bat so both builds can run side by side.
rem ---------------------------------------------------------------------
set "HOST=127.0.0.1"
set "PORT=8081"
start "" http://127.0.0.1:%PORT%
%PY% btweb.py --db bt.db --host %HOST% --port %PORT% --no-tasks --no-delete %*
echo.
pause
