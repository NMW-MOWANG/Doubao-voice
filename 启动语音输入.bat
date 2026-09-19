@echo off
rem Start Doubao Voice Input without a console window.
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "voice_input.py"
    exit /b
)

where pyw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pyw.exe "voice_input.py"
    exit /b
)

where python.exe >nul 2>nul
if %errorlevel%==0 (
    python.exe "voice_input.py"
    exit /b
)

echo.
echo Python was not found. Please install Python 3.8+ and add it to PATH.
echo Download: https://www.python.org/downloads/windows/
echo.
pause
