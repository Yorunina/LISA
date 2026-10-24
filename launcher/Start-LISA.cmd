@echo off
setlocal
cd /d "%~dp0.."
if not exist "runtime\venv\Scripts\python.exe" (
    echo LISA environment is missing. Run scripts\dev.ps1 Setup first.
    pause
    exit /b 1
)
"runtime\venv\Scripts\python.exe" "scripts\start_lisa.py" %*
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)
