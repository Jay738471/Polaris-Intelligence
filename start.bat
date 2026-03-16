@echo off
title AI Market Sentiment — NQ1 & ES1

echo.
echo  =====================================================
echo    AI MARKET SENTIMENT  -  NQ1 ^& ES1 Analyzer
echo  =====================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Please install Python 3.9+
    echo  Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Install dependencies if needed
echo  Installing/checking dependencies...
pip install -q flask requests

echo.
echo  Starting server at http://localhost:5000
echo  Opening browser...
echo.

:: Open browser after a short delay (run in background)
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"

:: Start the server
python server.py

pause
