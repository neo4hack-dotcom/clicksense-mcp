@echo off
:: ClickSense - the MCP server over streamable HTTP, in the foreground (Ctrl+C to stop).
:: Address and tokens come from .env: CLICKSENSE_HTTP_HOST (127.0.0.1 by default), CLICKSENSE_HTTP_PORT
:: (3110) and CLICKSENSE_HTTP_TOKENS. Listening on the network requires a token.
:: Extra arguments are passed through, e.g. `serve-windows.bat --host 0.0.0.0`.
:: No --reload, on purpose: on Windows it switches asyncio to the Selector loop.
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Run install-windows.bat first.
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m clicksense --config "%~dp0clicksense.yaml" --transport http %*
exit /b %errorlevel%
