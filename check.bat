@echo off
cd /d %~dp0
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
.venv\Scripts\python -m engine check
pause
