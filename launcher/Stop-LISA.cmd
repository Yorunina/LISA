@echo off
setlocal
cd /d "%~dp0.."
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "scripts\stop_lisa.ps1" %*
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)
