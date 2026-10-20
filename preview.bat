@echo off
cd /d %~dp0
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
.venv\Scripts\python -m engine preview %1 -n 3
pause
