@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" dashboard.py --open
    exit /b
)
where python.exe >nul 2>nul
if not errorlevel 1 (
    python.exe dashboard.py --open
    exit /b
)
py.exe -3 dashboard.py --open
exit /b %errorlevel%
