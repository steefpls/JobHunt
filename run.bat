@echo off
setlocal EnableExtensions
rem Starts JobHunt and opens it in your web browser (http://127.0.0.1:8765).
rem Keep this window open while you use JobHunt; close it to stop.
cd /d "%~dp0"
title JobHunt
if not exist ".venv\Scripts\python.exe" (
    echo JobHunt is not set up yet. Double-click setup.bat first.
    pause
    exit /b 1
)
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.browsers"
rem Fetch the latest JobHunt from GitHub first (skipped when offline, or when JobHunt is already running).
".venv\Scripts\python.exe" update.py %*
rem An update can't rewrite this file while it runs, so it leaves run.bat.new:
rem swap it in and start again with the new one. (One block: cmd reads it whole.)
if exist "run.bat.new" (
    move /y "run.bat.new" "run.bat" >nul
    call "%~f0" %*
    exit /b
)
".venv\Scripts\python.exe" -m jobhunt %*
if errorlevel 1 (
    echo.
    echo JobHunt stopped with an error (see above^).
    echo If it says the port is in use, JobHunt is probably already running: check your browser.
    pause
)
