@echo off
rem Double-click this file to start Signal Identifier.
title Signal Identifier
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

if not exist ".venv\Scripts\python.exe" (
    echo First-time setup: getting things ready. This takes a minute or two...
    %PY% -m venv .venv
    if errorlevel 1 goto failed
)

echo Checking that everything is installed...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto failed

echo Starting Signal Identifier. Keep this black window open while you use it.
".venv\Scripts\python.exe" -m signal_identifier
if errorlevel 1 goto failed
exit /b 0

:nopython
echo.
echo  Python is not installed on this computer (or Windows can't find it).
echo.
echo  1. Go to https://www.python.org/downloads/ and click the big yellow Download button.
echo  2. Run the installer. IMPORTANT: tick the box "Add python.exe to PATH" at the bottom.
echo  3. Click "Install Now". When it finishes, double-click this file again.
echo.
pause
exit /b 1

:failed
echo.
echo  Something went wrong. Please take a screenshot of this window and send it to whoever helped you set this up.
echo.
pause
exit /b 1
