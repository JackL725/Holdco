@echo off
rem Make Holdco start hidden in the background every time you log in to Windows.
rem To undo:  autostart_windows.bat uninstall
setlocal
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if "%~1"=="" (%PY% autostart.py install) else (%PY% autostart.py %1)
pause
