@echo off
:: ClickSense - chat with the data through an OpenAI-compatible LLM (key and model in .env:
:: OPENAI_API_KEY, OPENAI_BASE_URL, CLICKSENSE_LLM_MODEL). Extra arguments are passed through,
:: e.g. `chat-windows.bat -q "Revenue by channel last month"`.
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Run install-windows.bat first.
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m clicksense.chat --config "%~dp0clicksense.yaml" %*
exit /b %errorlevel%
