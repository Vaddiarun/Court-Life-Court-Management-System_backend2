@echo off
cd /d "%~dp0"
title Court Life Backend
echo ===== Court Life backend (local, no Docker) =====
if not exist ".env" copy /Y "env.generated" ".env" >nul
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating Python 3.12 virtual environment ...
  py -3.12 -m venv .venv || python -m venv .venv
)
echo [2/3] Installing dependencies (first time takes 2-4 minutes) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt pgserver > pip_log.txt 2>&1
if errorlevel 1 (
  echo PIP_FAILED - see pip_log.txt
  type pip_log.txt
  pause
  exit /b 1
)
echo [3/3] Starting database and API ...
".venv\Scripts\python.exe" run_local.py
pause
