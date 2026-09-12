@echo off
rem ---------------------------------------------------------------------
rem  Migrates an existing database to a contentless FTS index, dropping
rem  the copy of the indexed text that nothing ever reads -- roughly
rem  47 percent of the file. Databases created by the current code are
rem  already contentless; this is only for older ones.
rem
rem  Stop the crawler and the web UI first: the migration needs exclusive
rem  write access. Ctrl-C is safe -- run it again and it resumes, and the
rem  old index stays untouched until the final swap.
rem
rem  Keep this file pure ASCII: btcheck.py reads the .bat launchers as
rem  ASCII and silently skips any that are not.
rem ---------------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
call _env.bat
if errorlevel 1 (
  pause
  exit /b 1
)
if "%~1"=="" (set "DB=bt.db") else (set "DB=%~1")
echo.
echo   Estimating first. Nothing is written in this step.
echo.
%PY% btmigrate.py --db "%DB%"
echo.
set /p "YES=Migrate for real? Type yes and press Enter: "
if /i not "%YES%"=="yes" (
  echo   Cancelled, nothing was changed.
  echo.
  pause
  exit /b 0
)
echo.
%PY% btmigrate.py --db "%DB%" --go
echo.
pause
