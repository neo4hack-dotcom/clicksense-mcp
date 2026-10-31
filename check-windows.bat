@echo off
:: ClickSense - configuration and connectivity report (ClickHouse reachability, readonly level,
:: limits, tables, semantic layer). Changes nothing.
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Run install-windows.bat first.
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m clicksense --config "%~dp0clicksense.yaml" --check
exit /b %errorlevel%
