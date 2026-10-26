@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create .venv with Python 3.12 and install requirements.txt first.
  echo See README.md.
  pause
  exit /b 1
)
echo Starting the integrated application from:
echo %CD%
".venv\Scripts\python.exe" -B tools\launch_interface.py
if errorlevel 1 pause
