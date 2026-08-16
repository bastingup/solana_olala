@echo off
rem Run the full Solana-olala test suite inside the project venv.
cd /d "%~dp0.."

if not exist "backend\.venv" (
    python -m venv backend\.venv
    backend\.venv\Scripts\pip install --quiet -r backend\requirements.txt
)
backend\.venv\Scripts\pip install --quiet -r tests\requirements-dev.txt

backend\.venv\Scripts\python -m pytest tests\ %*
