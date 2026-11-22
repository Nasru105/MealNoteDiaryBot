@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements.txt
if errorlevel 1 exit /b 1
ollama pull qwen3:4b
if errorlevel 1 exit /b 1
echo Ready. Run start_console.cmd for the local demo.
pause
