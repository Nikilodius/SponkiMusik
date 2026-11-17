@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.10+ was not found. Install Python from https://www.python.org/downloads/
    pause
    exit /b 1
)
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)
python app.py
if errorlevel 1 pause
