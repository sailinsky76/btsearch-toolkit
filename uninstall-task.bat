@echo off
chcp 65001 >nul
schtasks /delete /tn "BTIndex-Daily"  /f
schtasks /delete /tn "BTIndex-Weekly" /f
echo.
pause
