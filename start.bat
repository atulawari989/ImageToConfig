@echo off
rem ============================================================
rem  ImageToConfig - double-click launcher
rem  Starts the Flask app and opens it in your default browser.
rem ============================================================
title ImageToConfig
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found at .venv
    echo Run setup first:  python -m venv .venv  ^&  .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Starting ImageToConfig...
echo Opening http://127.0.0.1:5000 in your browser.
echo.
echo Keep this window open while you use the app.
echo Close it (or press Ctrl+C) to stop the server.
echo ============================================================

rem Give the server a moment to boot, then open the browser.
start "" /b cmd /c "timeout /t 2 >nul & start http://127.0.0.1:5000"

".venv\Scripts\python.exe" app.py

echo.
echo Server stopped.
pause
