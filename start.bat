@echo off
setlocal

set SCRIPT_DIR=%~dp0
set BACKEND_DIR=%SCRIPT_DIR%backend
set VENV_DIR=%SCRIPT_DIR%.venv

echo ==^> InstaGrow

REM Check Python is installed
where python >nul 2>nul
if errorlevel 1 (
    echo Error: Python is not installed or not in PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Create venv if needed
if not exist "%VENV_DIR%" (
    echo ==^> Creating virtualenv...
    python -m venv "%VENV_DIR%"
)

REM Activate venv
call "%VENV_DIR%\Scripts\activate.bat"

REM Install dependencies
echo ==^> Installing dependencies...
python -m pip install --upgrade pip >nul
pip install -q -r "%BACKEND_DIR%\requirements.txt"

echo.
echo ==^> Starting InstaGrow at http://localhost:8000
echo ==^> Press Ctrl+C to stop
echo.

cd /d "%BACKEND_DIR%"
uvicorn main:app --host 0.0.0.0 --port 8000

endlocal
