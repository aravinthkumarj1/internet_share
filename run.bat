@echo off
:: Internet Share Launcher — runs with admin elevation
:: Check for admin privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process -FilePath '%~dp0run.bat' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
echo Starting Internet Share...
python -m internet_share.app
pause
