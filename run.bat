@echo off
REM Start the dashboard at http://127.0.0.1:8050 (sets up .venv on first run)
cd /d "%~dp0"
where py >nul 2>nul && (py -3 start.py) || (python start.py)
pause
