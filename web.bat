@echo off
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 (
  pause
  exit /b 1
)
start "" http://127.0.0.1:8080
%PY% btweb.py --db bt.db --port 8080 %*
echo.
pause
