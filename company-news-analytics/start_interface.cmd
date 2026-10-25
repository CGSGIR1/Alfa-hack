@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create .venv with Python 3.12 and install requirements.txt first.
  echo See README.md for setup instructions.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" start_ui.py
if errorlevel 1 pause
