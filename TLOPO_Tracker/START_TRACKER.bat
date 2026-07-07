@echo off
title TLOPO Loot Tracker - EXPERIMENTAL-ALPHA (untested multi-window)
color 0B

echo ============================================================
echo   Starting TLOPO Loot Tracker -- EXPERIMENTAL-ALPHA (untested multi-window)
echo ============================================================
echo.

if not exist "%~dp0venv\Scripts\activate.bat" (
    echo   It looks like setup has not been run yet.
    echo   Please double-click install.bat first, then try again.
    echo.
    pause
    exit /b 1
)

call "%~dp0venv\Scripts\activate.bat"

echo   If this is the very first time launching, the OCR engine will
echo   download a small language model ^(about 100MB^). This only
echo   happens once and may take a minute depending on your internet.
echo.

python "%~dp0tlopo_tracker.py"

if errorlevel 1 (
    echo.
    echo ============================================================
    echo   The tracker closed unexpectedly. The error is shown above.
    echo   Keep this window open and share the error if you need help.
    echo ============================================================
    echo.
    pause
)
