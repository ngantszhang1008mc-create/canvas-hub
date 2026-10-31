@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-canvas-hub.ps1" %*
exit /b %errorlevel%
