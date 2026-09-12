@echo off
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 (
  pause
  exit /b 1
)
if "%~1"=="" (
  echo Usage:  search.bat KEYWORD
  echo Example: search.bat ubuntu
  echo.
  pause
  exit /b 1
)
%PY% btindex.py --db bt.db search %*
echo.
pause
