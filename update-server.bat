@echo off
rem Sends code and channel configs to the VPS and restarts the engine. .env and the database on the server are kept.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\update.ps1" -Log "%~dp0update-log.txt"
echo.
echo DONE. Log: update-log.txt
pause
