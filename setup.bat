@echo off
rem ScrapeLite — one-time setup. Creates the virtual environment, installs
rem packages, and makes sure Ollama + the model are present.
rem
rem Dry run (show what would happen, change nothing):
rem     setup.bat --dry-run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
echo.
pause
