@echo off
rem Saves the server journal to server-log.txt (read only, nothing changes on the server).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\logs.ps1" -Log "%~dp0server-log.txt"
echo DONE
timeout /t 5
