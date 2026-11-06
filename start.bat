@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run: python -m venv .venv
  pause
  exit /b 1
)
".venv\Scripts\python.exe" desktop.py
if errorlevel 1 pause
