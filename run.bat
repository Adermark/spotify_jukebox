@echo off
rem Start the jukebox with its own Python environment (.venv), creating it on first run.
rem The browser opens by itself: the setup page the first time, then the admin page.
rem Close this window (or press Ctrl+C) to stop the jukebox.
cd /d "%~dp0"

if exist .venv\Scripts\python.exe goto install

rem --- find Python 3.11+ (the "py" launcher first; "python" may be the Microsoft Store stub) ---
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "PY=python"
if not defined PY goto nopython

echo Setting up the jukebox, this takes a minute the first time...
%PY% -m venv .venv || goto failed

:install
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt || goto failed
.venv\Scripts\python run.py
if errorlevel 1 (
    echo.
    echo The jukebox stopped. If that wasn't you, see the message above.
    pause
)
exit /b 0

:nopython
echo.
echo The jukebox needs Python 3.11 or newer, and it wasn't found on this computer.
where winget >nul 2>&1 || goto manualpython
echo.
choice /c YN /m "Install Python now (free, from Microsoft's winget)"
if errorlevel 2 goto manualpython
winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements || goto manualpython
echo.
echo Python is installed. Close this window and double-click run.bat again.
pause
exit /b 0

:manualpython
echo.
echo Install Python from https://www.python.org/downloads/
echo In the installer, tick "Add python.exe to PATH". Then double-click run.bat again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:failed
echo.
echo The jukebox could not start. See the message above.
pause
exit /b 1
