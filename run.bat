@echo off
rem Solana-olala launcher (Windows). Creates the venv on first run,
rem installs dependencies, starts the backend (which serves the frontend),
rem and opens the dashboard.
cd /d "%~dp0"

if not exist "backend\.venv" (
    python -m venv backend\.venv
    if errorlevel 1 (
        echo Failed to create the virtual environment. Is Python installed?
        exit /b 1
    )
)

backend\.venv\Scripts\pip install --quiet --requirement backend\requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    exit /b 1
)

start "" "http://127.0.0.1:8420"
backend\.venv\Scripts\python backend\run.py
