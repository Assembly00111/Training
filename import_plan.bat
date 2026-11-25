@echo off
rem Drag your training plan's .md file onto this file to import it.
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo.
  echo Drag your training plan's .md file onto import_plan.bat to import it.
  echo.
  pause
  exit /b 1
)
python plan_import.py "%~1"
echo.
pause
