@echo off
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 (
  pause
  exit /b 1
)
echo Using: %PY%
echo.
%PY% btcheck.py
echo.
pause
