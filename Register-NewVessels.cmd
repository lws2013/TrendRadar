@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH.
    echo Install from https://www.python.org/downloads/
    echo Check "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

python register_new_vessels.py %*
