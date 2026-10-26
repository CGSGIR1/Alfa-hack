@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create .venv with Python 3.12 and install requirements.txt first.
  pause
  exit /b 1
)
echo Opening the separate management interface...
".venv\Scripts\python.exe" -B tools\launch_interface.py --view manage
if errorlevel 1 pause
