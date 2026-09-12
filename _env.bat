@echo off
rem ---------------------------------------------------------------------
rem  Python launcher used by every .bat in this folder.
rem  This machine runs Python 3.11 through the "py" launcher.
rem  Using a different version later? Just edit the first line below.
rem ---------------------------------------------------------------------
set "PY=py -3.11"
%PY% -c "" >nul 2>&1 && goto :found

rem Fallbacks, in case 3.11 is gone or the launcher is missing.
set "PY=py -3"
%PY% -c "" >nul 2>&1 && goto :found
set "PY=python"
%PY% -c "" >nul 2>&1 && goto :found

echo.
echo   Cannot find Python. Install Python 3.8 or newer from python.org
echo   and tick "Add Python to PATH" during setup.
echo.
exit /b 1

:found
exit /b 0
