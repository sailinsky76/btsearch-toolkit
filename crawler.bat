@echo off
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 (
  pause
  exit /b 1
)
echo.
echo   DHT crawler starting (with active DHT lookups).
echo   Keep this window open.
echo   Press Ctrl-C to stop.
echo.
%PY% dhtmeta.py --sniff --db bt.db --port 6881 --workers 60 --with-lookup --lookup-workers 30 %*
echo.
pause
