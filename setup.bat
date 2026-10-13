@echo off
setlocal EnableExtensions
rem ===================================================================
rem  JobHunt setup. Safe to run again at any time (it only fixes what
rem  is missing). Everything goes into this folder:
rem    .venv\       Python packages for JobHunt only
rem    .browsers\   the Chromium browser JobHunt drives
rem  Nothing else on your PC is changed, apart from installing Python
rem  itself if you say yes to that.
rem ===================================================================
cd /d "%~dp0"
title JobHunt setup
echo.
echo  JobHunt setup
echo  =============
echo.

rem ---- 1. Python 3.11 or newer ---------------------------------------
echo [1/4] Looking for Python 3.11 or newer...
call :find_python
if defined PY goto have_python

echo.
echo  Python 3.11+ was not found on this PC.
where winget >nul 2>nul
if errorlevel 1 goto no_winget
choice /C YN /M "  Install Python 3.12 now (uses Windows' own winget installer)"
if errorlevel 2 goto no_python
winget install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements
call :find_python
if defined PY goto have_python
echo.
echo  Python was installed, but this window can't see it yet.
echo  Close this window and double-click setup.bat again.
goto fail

:no_winget
echo  Please install Python from https://www.python.org/downloads/
echo  (tick "Add python.exe to PATH" in the installer), then run setup.bat again.
goto fail

:no_python
echo  Setup needs Python. Install it, then run setup.bat again.
goto fail

:have_python
for /f "delims=" %%v in ('call "%PY%" -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo       Found Python %PYVER%: %PY%

rem ---- 2. The private Python environment (.venv) -------------------
echo.
echo [2/4] Preparing JobHunt's own Python environment (.venv)...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo       The existing .venv is broken or too old; making a new one.
        rmdir /s /q ".venv"
    )
)
if not exist ".venv\Scripts\python.exe" (
    "%PY%" -m venv .venv
    if errorlevel 1 (
        echo  Could not create the .venv folder. Is this folder writable?
        goto fail
    )
)
set "VPY=%~dp0.venv\Scripts\python.exe"

echo.
echo [3/4] Installing Python packages (first time: a few minutes)...
"%VPY%" -m pip install --disable-pip-version-check --no-input -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo  Installing packages failed. Check your internet connection and run setup.bat again.
    goto fail
)
"%VPY%" -c "import keyring; keyring.get_keyring()" >nul 2>nul
if errorlevel 1 (
    echo  Warning: Windows Credential Manager could not be opened. Saving API keys may fail.
)

rem ---- 3. Chromium for the job sites (kept inside this folder) -------
echo.
echo [4/4] Installing the Chromium browser JobHunt uses (about 700 MB the first time)...
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.browsers"
"%VPY%" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo  Downloading Chromium failed. Check your internet connection and run setup.bat again.
    goto fail
)

echo.
echo  ==========================================================
echo   Setup finished. Double-click run.bat to start JobHunt.
echo  ==========================================================
echo.
if not defined JOBHUNT_NO_PAUSE pause
exit /b 0

:fail
echo.
echo  Setup did NOT finish (see the message above).
echo.
if not defined JOBHUNT_NO_PAUSE pause
exit /b 1

rem ---- helpers ---------------------------------------------------------
:find_python
set "PY="
rem The py launcher, if Python came from python.org
for %%c in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%c -c "import sys" >nul 2>nul && for /f "delims=" %%p in ('py -%%c -c "import sys; print(sys.executable)"') do set "PY=%%p"
    )
)
if defined PY exit /b 0
rem python on PATH (skips the Microsoft Store stand-in, which fails this check)
for /f "delims=" %%p in ('python -c "import sys; sys.version_info >= (3, 11) and print(sys.executable)" 2^>nul') do set "PY=%%p"
if defined PY exit /b 0
rem the usual per-user install folders (e.g. right after winget, before PATH updates)
for %%d in (Python313 Python312 Python311) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\%%d\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\%%d\python.exe"
)
exit /b 0
