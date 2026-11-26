@echo off
rem Double-click this file to start Signal Identifier.
rem The first time, it also puts a "Signal Identifier" icon on your Desktop:
rem that icon starts the program without any black window.
title Signal Identifier
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"

if not exist ".venv\Scripts\pythonw.exe" (
    if not defined PY goto nopython
    echo First-time setup: getting things ready. This takes a minute or two...
    %PY% -m venv .venv
    if errorlevel 1 goto failed
)

rem Only install or update packages when requirements.txt has changed.
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>nul
if errorlevel 1 (
    echo Checking that everything is installed...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 goto failed
    copy /y requirements.txt ".venv\installed-requirements.txt" >nul
)

rem Desktop icon that starts the program with no black window (made once).
if not exist ".venv\desktop-icon-made" call :makeicon

rem Start the program without a console window, then close this one.
start "" ".venv\Scripts\pythonw.exe" -m signal_identifier
exit /b 0

:makeicon
powershell -NoProfile -ExecutionPolicy Bypass -Command "$here=(Get-Location).Path; $lnk=Join-Path ([Environment]::GetFolderPath('Desktop')) 'Signal Identifier.lnk'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($lnk); $s.TargetPath=Join-Path $here '.venv\Scripts\pythonw.exe'; $s.Arguments='-m signal_identifier'; $s.WorkingDirectory=$here; $s.IconLocation=(Join-Path $here 'signal_identifier\icon.ico')+',0'; $s.Description='Signal Identifier'; $s.Save()" >nul 2>nul
if not errorlevel 1 echo made> ".venv\desktop-icon-made"
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
