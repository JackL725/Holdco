@echo off
rem Start Holdco and open the dashboard. Keep this window open while agents work.
setlocal
cd /d "%~dp0"
title Holdco

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Holdco needs Python 3.10 or newer.
  echo Download it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" while installing.
  pause
  exit /b 1
)

%PY% run.py %*
pause
