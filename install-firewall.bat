@echo off
chcp 65001 >nul
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo   This needs Administrator rights.
  echo   Right-click this file and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)
netsh advfirewall firewall add rule name="BT DHT Crawler" dir=in action=allow protocol=UDP localport=6881-6888
echo.
echo Inbound UDP 6881-6888 allowed. The crawler needs this to receive queries.
echo.
pause
