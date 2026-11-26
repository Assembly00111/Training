@echo off
rem Reads plans you send to "My laptop" from the dashboard.
rem The dashboard opens this for you (training-worker:// link); it reads one plan and closes.
chcp 65001 >nul
title Plan worker
cd /d "%~dp0"
if "%~1"=="" (
  python plan_worker.py
  echo.
  pause
  exit /b
)
python plan_worker.py --launched
echo.
echo This window closes in 30 seconds.
timeout /t 30 >nul
