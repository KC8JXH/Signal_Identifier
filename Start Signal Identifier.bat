@echo off
rem Double-click this file to install (or update) and start Signal Identifier.
rem It copies the program to INSTALL_DIR below and runs it from there, so the
rem downloaded folder can be deleted afterwards. It also puts a
rem "Signal Identifier" icon on your Desktop that starts the program without
rem any black window.
title Signal Identifier
set "INSTALL_DIR=C:\SignalIdentifier"
cd /d "%~dp0"

rem Running from a downloaded copy: install / update it in INSTALL_DIR first.
rem Keeps what's already there (Python setup, recordings); copies new files over.
if /i not "%CD%"=="%INSTALL_DIR%" (
    echo Installing Signal Identifier in %INSTALL_DIR% ...
    robocopy "%CD%" "%INSTALL_DIR%" /E /XD .venv .git __pycache__ .pytest_cache /XF signal_identifier_error.log /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto copyfailed
    cd /d "%INSTALL_DIR%"
)

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"

rem If the folder was moved, the old setup may not work here: start it afresh.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if errorlevel 1 (
        echo This folder seems to have been moved. Setting things up again...
        rmdir /s /q ".venv"
    )
)

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

rem Desktop icon that starts the program with no black window. Made again
rem if this folder has moved since, so the icon always points here.
set "MADE_FOR="
if exist ".venv\desktop-icon-made" set /p MADE_FOR=<".venv\desktop-icon-made"
if /i not "%MADE_FOR%"=="%CD%" call :makeicon

rem Start the program without a console window, then close this one.
start "" ".venv\Scripts\pythonw.exe" -m signal_identifier
exit /b 0

:makeicon
powershell -NoProfile -ExecutionPolicy Bypass -Command "$here=(Get-Location).Path; $lnk=Join-Path ([Environment]::GetFolderPath('Desktop')) 'Signal Identifier.lnk'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($lnk); $s.TargetPath=Join-Path $here '.venv\Scripts\pythonw.exe'; $s.Arguments='-m signal_identifier'; $s.WorkingDirectory=$here; $s.IconLocation=(Join-Path $here 'signal_identifier\icon.ico')+',0'; $s.Description='Signal Identifier'; $s.Save()" >nul 2>nul
if not errorlevel 1 >".venv\desktop-icon-made" echo %CD%
exit /b 0

:copyfailed
echo.
echo  Couldn't copy the program to %INSTALL_DIR%.
echo  If Signal Identifier is running, close it and double-click this file again.
echo.
pause
exit /b 1

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
