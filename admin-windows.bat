@echo off
:: ClickSense - maintenance commands on Windows (clicksense-admin). Examples:
::   admin-windows.bat kb validate --live
::   admin-windows.bat kb bootstrap shop.orders
::   admin-windows.bat kb new metric net_revenue
::   admin-windows.bat kb search "revenue by channel last month"
::   admin-windows.bat config --show
:: Changes under semantic\ are picked up by the running server within seconds; changes to
:: clicksense.yaml or .env need: powershell -ExecutionPolicy Bypass -File scripts\windows\service.ps1 restart
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Run install-windows.bat first.
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m clicksense.admin --config "%~dp0clicksense.yaml" %*
exit /b %errorlevel%
