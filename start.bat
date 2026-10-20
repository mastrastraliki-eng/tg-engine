@echo off
REM Main loop. Keep this window open while channels should run.
cd /d %~dp0
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
.venv\Scripts\python -m engine run
pause
