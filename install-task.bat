@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo Registering two scheduled tasks:
echo   BTIndex-Daily    every day 04:00  - health check + peer probing
echo   BTIndex-Weekly   Sunday    05:00  - also prune dead entries + vacuum
echo.
schtasks /create /tn "BTIndex-Daily"  /tr "\"%~dp0maint.bat\""        /sc daily          /st 04:00 /f
schtasks /create /tn "BTIndex-Weekly" /tr "\"%~dp0maint-weekly.bat\"" /sc weekly /d SUN /st 05:00 /f
echo.
echo Done. Check them with:  schtasks /query /tn BTIndex-Daily
echo Remove them with:       schtasks /delete /tn BTIndex-Daily /f
echo.
pause
