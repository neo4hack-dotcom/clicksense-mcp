@echo off
:: ClickSense - Windows install (run once; double-click or `install-windows.bat`).
:: Same model as DOINg.AI: a local .venv, pip install -r requirements.txt, configuration in .env.
:: Offline / corporate networks: pip uses the mirror declared in pip.ini.
setlocal EnableExtensions
cd /d "%~dp0"
echo [ClickSense] Installing for Windows...

:: --- Python 3.11+: the py launcher first, then python on PATH ---
set "PY="
for %%C in ("py -3" "python" "python3") do (
    if not defined PY (
        %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=%%~C"
    )
)
if not defined PY (
    echo [ERROR] Python 3.11 or newer was not found.
    echo         Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH";
    echo         on a server, choose "Install for all users" so the service account can use it^).
    exit /b 1
)
echo [OK] Python: %PY%

:: --- Long paths: site-packages can exceed 260 characters ---
reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled 2>nul | find "0x1" >nul
if errorlevel 1 (
    reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f >nul 2>&1
    if errorlevel 1 (
        echo [WARNING] Administrator rights are needed to enable long paths.
        echo           Run this script as Administrator if the install fails on long paths.
    ) else (
        echo [OK] Long paths enabled.
    )
) else (
    echo [OK] Long paths already enabled.
)

:: --- Virtual environment and dependencies ---
if not exist ".venv\Scripts\python.exe" (
    echo [ClickSense] Creating .venv...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        exit /b 1
    )
)
set "VPY=%~dp0.venv\Scripts\python.exe"
"%VPY%" -m pip install -q --upgrade pip
"%VPY%" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed. Offline: check the internal PyPI mirror in pip.ini.
    exit /b 1
)
"%VPY%" -m pip install -q --no-deps -e .
if errorlevel 1 (
    echo [ERROR] Could not install the clicksense package.
    exit /b 1
)
echo [OK] Python dependencies installed.

:: --- Configuration: created once, never overwritten ---
if not exist "clicksense.yaml" (
    copy /y "clicksense.example.yaml" "clicksense.yaml" >nul
    echo [OK] clicksense.yaml created from clicksense.example.yaml.
)
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo [OK] .env created from .env.example.
)
findstr /b /c:"CLICKSENSE_HTTP_TOKENS=" ".env" >nul 2>&1
if errorlevel 1 (
    for /f "delims=" %%T in ('"%VPY%" -m clicksense --gen-token') do (
        >>".env" echo CLICKSENSE_HTTP_TOKENS=%%T
    )
    echo [OK] A bearer token for MCP clients was generated in .env ^(CLICKSENSE_HTTP_TOKENS^).
)

echo.
echo [ClickSense] Connection check ^(edit .env if ClickHouse is not reachable^):
set PYTHONUTF8=1
"%VPY%" -m clicksense --config "%~dp0clicksense.yaml" --check
echo.
echo [OK] Install complete.
echo      1. Edit .env: ClickHouse host, user and password ^(CLICKSENSE_HOST, ...^).
echo      2. check-windows.bat    - test the connection again
echo      3. serve-windows.bat    - run the MCP server over HTTP ^(foreground^)
echo      4. As Administrator: powershell -ExecutionPolicy Bypass -File scripts\windows\service.ps1 install
echo                              - run it at boot as a scheduled task
echo      chat-windows.bat     - ask questions through an OpenAI-compatible LLM
endlocal
