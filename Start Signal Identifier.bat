@echo off
rem ===========================================================================
rem  Signal Identifier: double-click to install (or update) and start it.
rem
rem  * Installs the program in INSTALL_DIR (below) and starts it from there.
rem  * Puts a "Signal Identifier" icon on the Desktop that starts it with no
rem    black window.
rem  * Installs Python first if the computer doesn't have it.
rem  * Afterwards deletes the downloaded copy (this folder and its ZIP), so
rem    nothing is left lying around.
rem ===========================================================================
title Signal Identifier
set "INSTALL_DIR=C:\SignalIdentifier"
cd /d "%~dp0"

rem Opened straight from inside the ZIP (not unzipped)? Then only this one file
rem is here and the program files are missing.
if not exist "signal_identifier\__main__.py" goto notextracted

rem ---- 1. copy the program into INSTALL_DIR (unless we're already there) ----
set "CLEANUP="
if /i not "%CD%"=="%INSTALL_DIR%" (
    echo Installing Signal Identifier in %INSTALL_DIR% ...
    robocopy "%CD%" "%INSTALL_DIR%" /E /XD .venv .git __pycache__ .pytest_cache /XF signal_identifier_error.log /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto copyfailed
    set "CLEANUP=%CD%"
    cd /d "%INSTALL_DIR%"
)

rem ---- 2. find Python (or install it) -----------------------------------------
set "PY="
call :findpython
rem A Python setup that no longer runs, for example after Python was removed: start afresh.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if errorlevel 1 rmdir /s /q ".venv"
)
if not exist ".venv\Scripts\pythonw.exe" if not defined PY call :installpython
if not exist ".venv\Scripts\pythonw.exe" if not defined PY goto nopython

rem ---- 3. the program's own Python setup ----------------------------------------
if not exist ".venv\Scripts\pythonw.exe" (
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

rem ---- 4. Desktop icon (made again if the install folder changed) -----------
set "MADE_FOR="
if exist ".venv\desktop-icon-made" set /p MADE_FOR=<".venv\desktop-icon-made"
if /i not "%MADE_FOR%"=="%CD%" call :makeicon

rem ---- 5. start the program (no console window), tidy up, close ------------
start "" ".venv\Scripts\pythonw.exe" -m signal_identifier
if defined CLEANUP call :cleanup
exit /b 0


rem ============================== helpers ======================================

:findpython
rem Use Python only if it really runs (the Microsoft Store "python" shortcut doesn't).
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (set "PY=py -3" & exit /b 0)
python -c "import sys" >nul 2>nul
if not errorlevel 1 (set "PY=python" & exit /b 0)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
exit /b 0

:installpython
where winget >nul 2>nul
if errorlevel 1 exit /b 0
echo Python isn't installed on this computer yet. Installing it now.
echo This can take a few minutes. Please wait...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements >nul 2>nul
call :findpython
exit /b 0

:makeicon
powershell -NoProfile -ExecutionPolicy Bypass -Command "$here=(Get-Location).Path; $lnk=Join-Path ([Environment]::GetFolderPath('Desktop')) 'Signal Identifier.lnk'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($lnk); $s.TargetPath=Join-Path $here '.venv\Scripts\pythonw.exe'; $s.Arguments='-m signal_identifier'; $s.WorkingDirectory=$here; $s.IconLocation=(Join-Path $here 'signal_identifier\icon.ico')+',0'; $s.Description='Signal Identifier'; $s.Save()" >nul 2>nul
if not errorlevel 1 >".venv\desktop-icon-made" echo %CD%
exit /b 0

:cleanup
rem Delete the downloaded copy we installed from, and its ZIP. This file is
rem still running from that folder, so a small helper does it a few seconds
rem after this window has closed. Never touches anything that isn't a copy
rem of this program.
set "SRC=%CLEANUP%"
if /i "%SRC%"=="%USERPROFILE%" exit /b 0
if /i "%SRC%"=="%USERPROFILE%\Desktop" exit /b 0
if /i "%SRC%"=="%USERPROFILE%\Downloads" exit /b 0
if /i "%SRC%"=="%INSTALL_DIR%" exit /b 0
if not exist "%SRC%\signal_identifier\__main__.py" exit /b 0
if not exist "%SRC%\Start Signal Identifier.bat" exit /b 0
for %%I in ("%SRC%\..") do set "PARENT=%%~fI"
for %%I in ("%PARENT%") do set "PNAME=%%~nxI"
set "CB=%TEMP%\signal_identifier_cleanup.bat"
>"%CB%" echo @echo off
>>"%CB%" echo ping -n 4 127.0.0.1 ^>nul
>>"%CB%" echo rmdir /s /q "%SRC%"
rem "Extract All" often makes a folder inside a folder of the same name: remove the outer one if it's now empty.
if /i "%PNAME:~0,17%"=="Signal_Identifier" >>"%CB%" echo rmdir "%PARENT%" 2^>nul
>>"%CB%" echo del /q "%USERPROFILE%\Downloads\Signal_Identifier-main*.zip" 2^>nul
>>"%CB%" echo (goto) 2^>nul ^& del "%%~f0"
start "" /min "%CB%"
exit /b 0

:notextracted
echo.
echo  The download needs to be unzipped first.
echo.
echo   1. Close this window.
echo   2. Find the downloaded file "Signal_Identifier-main" (usually in Downloads).
echo   3. Right-click it and choose "Extract All...", then click "Extract".
echo   4. In the folder that opens, double-click "Start Signal Identifier".
echo.
pause
exit /b 1

:copyfailed
echo.
echo  Couldn't copy the program to %INSTALL_DIR%.
echo  If Signal Identifier is running, close it and double-click this file again.
echo.
pause
exit /b 1

:nopython
echo.
echo  Python couldn't be installed automatically.
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
