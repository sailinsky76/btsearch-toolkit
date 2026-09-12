@echo off
rem Weekly maintenance. This one deletes confirmed-dead torrents and reclaims disk.
rem It backs the database up first.
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 exit /b 1
if not exist logs mkdir logs
if not exist backup mkdir backup
%PY% btmaint.py --db bt.db --scan 800 --prune-dead --prune-dead-after 30 --vacuum --backup backup\bt-weekly.db --log logs\maint.log %*
exit /b %ERRORLEVEL%
