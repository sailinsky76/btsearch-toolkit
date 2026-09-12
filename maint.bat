@echo off
rem Daily maintenance. Non-destructive: health check, index repair, peer probing.
rem Called by Task Scheduler, so no pause here.
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 exit /b 1
if not exist logs mkdir logs
%PY% btmaint.py --db bt.db --scan 300 --log logs\maint.log %*
exit /b %ERRORLEVEL%
