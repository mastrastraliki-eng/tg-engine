@echo off
REM First run: creates .venv and installs dependencies
cd /d %~dp0
where py >nul 2>nul || (echo Install Python 3.12 from python.org and tick "Add python.exe to PATH" & pause & exit /b 1)
if not exist .venv py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
echo Done. Fill in .env, then run check.bat
pause
