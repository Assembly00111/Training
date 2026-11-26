@echo off
rem Reads plans you send to "My laptop" from the dashboard. Leave this window open.
chcp 65001 >nul
title Plan worker
cd /d "%~dp0"
python plan_worker.py
echo.
pause
