@echo off
rem ScrapeLite — double-click to launch the web app.
rem
rem Dry run (show what would happen, change nothing):
rem     start.bat --dry-run
cd /d "%~dp0"

set "DRY_RUN="
for %%A in (%*) do (
    if /I "%%~A"=="--dry-run" set "DRY_RUN=1"
    if /I "%%~A"=="-n" set "DRY_RUN=1"
    if /I "%%~A"=="--dryrun" set "DRY_RUN=1"
)

if not exist ".venv\Scripts\python.exe" (
    if defined DRY_RUN (
        echo [DRY-RUN] would trigger setup ^(venv missing: .venv\Scripts\python.exe^)
        echo [DRY-RUN] would launch: .venv\Scripts\python.exe app.py at http://127.0.0.1:5000 ^(after setup^)
        echo [DRY-RUN] Dry run complete - nothing was changed.
        pause
        exit /b 0
    )
    echo Virtual environment not found - running setup first...
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Setup did not complete. Please double-click setup.bat and read the messages.
    pause
    exit /b 1
)

if defined DRY_RUN (
    echo [DRY-RUN] venv present, setup skipped
    echo [DRY-RUN] would launch: .venv\Scripts\python.exe app.py at http://127.0.0.1:5000
    echo [DRY-RUN] Dry run complete - nothing was changed.
    pause
    exit /b 0
)

.venv\Scripts\python.exe app.py
pause
