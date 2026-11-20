@echo off
rem Pull new Garmin data and rebuild your local dashboard.
rem (The public site updates itself on GitHub every 3 hours.)
cd /d "%~dp0"
echo.
python pull.py
if errorlevel 1 (
  echo.
  echo Pull failed. If it says the key is wrong or expired, check .env.local.
  pause
  exit /b 1
)
echo.
python build.py
echo.
echo All done. Your dashboard is open in your browser.
timeout /t 5 >nul
