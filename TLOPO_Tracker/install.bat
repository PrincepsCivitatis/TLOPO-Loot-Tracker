@echo off
setlocal
title TLOPO Loot Tracker - Setup
color 0B

echo ============================================================
echo   TLOPO Loot Tracker - First Time Setup
echo ============================================================
echo.
echo This will set up everything the tracker needs to run.
echo This only needs to be done once. It may take several minutes
echo the first time, especially the OCR/AI library install.
echo.

REM ---------------------------------------------------------------
REM Step 1: Check for Python 3.10+
REM ---------------------------------------------------------------
echo [1/5] Checking for Python...

REM Prefer the Windows "py" launcher over a bare "python" on PATH --
REM "py -3" resolves to the NEWEST installed Python 3.x by default. A
REM bare "python" instead resolves to whatever happens to be first on
REM PATH, which with multiple Python versions installed (e.g. 3.8 AND
REM 3.13) can silently be the OLDER one, making a perfectly good 3.10+
REM install invisible to this check (GitHub issue #10). Falls back to
REM bare "python" for machines where the launcher isn't installed.
set PYCMD=
set PYVER=

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set PYCMD=py -3
        for /f "tokens=2" %%v in ('py -3 --version 2^>^&1') do set PYVER=%%v
    )
)

if not defined PYCMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set PYCMD=python
            for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
        )
    )
)

if not defined PYCMD (
    echo.
    echo ============================================================
    echo   Could not find an installed Python 3.10 or newer.
    echo.
    echo   If you already have Python 3.10+ installed alongside an
    echo   older version, make sure it's registered with the "py"
    echo   launcher -- on by default when installing from python.org --
    echo   or is the one "python --version" resolves to on your PATH.
    echo.
    echo   Otherwise, please install Python 3.10 or newer from:
    echo       https://www.python.org/downloads/
    echo.
    echo   IMPORTANT: During installation, check the box that says
    echo   "Add Python to PATH" before clicking Install.
    echo.
    echo   After installing Python, run this install.bat again.
    echo ============================================================
    echo.
    pause
    exit /b 1
)

echo     Found Python %PYVER% (using "%PYCMD%")
echo     Python version OK.
echo.

REM ---------------------------------------------------------------
REM Step 2: Create virtual environment
REM ---------------------------------------------------------------
echo [2/5] Setting up a private Python environment for the tracker...
if exist "%~dp0venv\Scripts\activate.bat" (
    echo     Environment already exists, skipping creation.
) else (
    %PYCMD% -m venv "%~dp0venv"
    if errorlevel 1 (
        echo.
        echo   Failed to create the virtual environment. See the error above.
        pause
        exit /b 1
    )
    echo     Environment created.
)
echo.

REM ---------------------------------------------------------------
REM Step 3: Activate environment and upgrade pip
REM ---------------------------------------------------------------
echo [3/5] Activating environment and preparing installer...
call "%~dp0venv\Scripts\activate.bat"
python -m pip install --upgrade pip >nul
echo     Ready.
echo.

REM ---------------------------------------------------------------
REM Step 4: Install CPU-only torch (required by easyocr)
REM ---------------------------------------------------------------
echo [4/5] Installing the OCR engine's AI backend (CPU-only, this is the
echo       biggest download and may take a few minutes)...
pip install torch --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 (
    echo.
    echo   Failed to install torch. Check your internet connection and
    echo   try running install.bat again.
    pause
    exit /b 1
)
echo     AI backend installed.
echo.

REM ---------------------------------------------------------------
REM Step 5: Install remaining dependencies
REM ---------------------------------------------------------------
echo [5/5] Installing remaining tracker dependencies...
pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo   Something went wrong installing dependencies. See the error above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Installation complete! Run START_TRACKER.bat to launch.
echo ============================================================
echo.
pause
