@echo off
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 (
  pause
  exit /b 1
)
echo.
echo   Bulk import from Internet Archive into bt.db
echo   Checking the API first...
echo.
%PY% btimport.py probe ia
echo.
%PY% btimport.py ia --limit 5000
echo.
pause
