@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\restart_interface.ps1" -StopOnly
if errorlevel 1 pause
