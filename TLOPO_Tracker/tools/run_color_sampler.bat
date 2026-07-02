@echo off
title TLOPO Loot Tracker - Color Sampler
color 0B

if "%~1"=="" (
    echo Drag and drop a screenshot image ^(PNG or JPG^) onto this file to
    echo see the exact colors it contains.
    echo.
    pause
    exit /b 1
)

set VENV_PY=%~dp0..\venv\Scripts\python.exe
if not exist "%VENV_PY%" (
    echo Could not find the tracker's Python environment.
    echo Please run install.bat in the TLOPO_Tracker folder first.
    echo.
    pause
    exit /b 1
)

"%VENV_PY%" "%~dp0color_sampler.py" "%~1"
echo.
pause
