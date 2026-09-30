@echo off
setlocal
cd /d "%~dp0"

set "FILTERTOOL=%~dp0.venv\Scripts\filtertool.exe"
if not exist "%FILTERTOOL%" (
    echo FilterTool executable not found:
    echo   %FILTERTOOL%
    echo Create the virtual environment and install the project first.
    pause
    exit /b 1
)

"%FILTERTOOL%" run --config "%~dp0config.yaml" --no-cache
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo FilterTool stopped with exit code %EXIT_CODE%.
    echo Check the output folder for run_manifest.json and search_log.csv.
)

pause
exit /b %EXIT_CODE%
