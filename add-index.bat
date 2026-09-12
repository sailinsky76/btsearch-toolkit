@echo off
rem ---------------------------------------------------------------------
rem  Creates the idx_source index on an existing database.
rem
rem  You probably do not need this. btindex.Index() runs the schema on
rem  every write-mode open, and the schema has CREATE INDEX IF NOT EXISTS
rem  for idx_source -- so one run of crawler.bat, import.bat or maint.bat
rem  already creates it. The web UI opens the database read-only and
rem  cannot create indexes, so this script is only for a database that
rem  is never opened by anything but the web UI.
rem
rem  Harmless to run twice. Roughly 3 seconds per 6 million rows.
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
echo   Creating idx_source on %DB% ...
echo.
%PY% -c "import sqlite3,sys,time; t=time.time(); c=sqlite3.connect(sys.argv[1]); c.execute('CREATE INDEX IF NOT EXISTS idx_source ON torrents(source)'); c.commit(); c.close(); print('done in', round(time.time()-t,1), 'seconds')" "%DB%"
echo.
pause
