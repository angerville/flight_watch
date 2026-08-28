@echo off
rem This project folder lives inside OneDrive, and OneDrive's background
rem sync (Files On-Demand / storage optimization) is known to corrupt or
rem empty out Python venvs - thousands of small site-packages files do not
rem survive cloud sync well, even on the SAME PC/account, not just when
rem moving to a new one.
rem
rem So the venv lives OUTSIDE OneDrive entirely, under %LOCALAPPDATA%
rem (per-Windows-account, never synced by OneDrive). %LOCALAPPDATA% also
rem auto-resolves to whichever account is logged in, so this still works
rem if the project folder syncs to a different PC/account.
rem
rem The health check actually imports the required packages (not just
rem checks that python.exe runs), so a silently-emptied venv gets rebuilt
rem automatically instead of crashing mid-run.
rem
rem Each launcher .bat calls this file first via `call`, then uses the
rem %VENV_PY% variable this script sets.
rem (Kept ASCII-only on purpose: chcp/codepage handling for non-ASCII text
rem  here has been unreliable across different invocation contexts.)

set "VENV_DIR=%LOCALAPPDATA%\flight_watch\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

"%VENV_PY%" -c "import yaml, requests, playwright" >nul 2>&1
if not errorlevel 1 goto :eof

echo ==============================================
echo   Python environment missing or incomplete on this PC.
echo   (Re)building it now outside OneDrive - first time only,
echo   may take a few minutes.
echo ==============================================

if exist "%VENV_DIR%" (
    rmdir /s /q "%VENV_DIR%"
)

set "PY_LAUNCHER="
where py >nul 2>&1
if not errorlevel 1 set "PY_LAUNCHER=py -3"
if not defined PY_LAUNCHER (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_LAUNCHER=python"
)
if not defined PY_LAUNCHER (
    echo.
    echo [ERROR] Python is not installed on this PC.
    echo         Install it from https://www.python.org/downloads/ and try again.
    echo         Make sure to check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

%PY_LAUNCHER% -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to create the virtual environment.
    pause
    exit /b 1
)

echo.
echo [setup] Installing required packages...
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check --upgrade pip
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo [ERROR] Package installation failed. Check your internet connection.
    pause
    exit /b 1
)
"%VENV_PY%" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo [ERROR] Playwright browser install failed. Check your internet connection.
    pause
    exit /b 1
)

echo [setup] Done.
echo.
