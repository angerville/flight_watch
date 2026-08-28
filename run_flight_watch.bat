@echo off
rem ---------------------------------------------------------------------
rem  Keep ALL text in this file ASCII-only (see _ensure_venv.bat comment
rem  for why: cmd.exe re-reads batch files by byte offset while running).
rem ---------------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem Task Scheduler captures no stdout/stderr by default, so log to a file
rem too -- otherwise a failure via the scheduler is undiagnosable.
call "%~dp0..\_ensure_venv.bat" >> "%~dp0task_run.log" 2>&1
if errorlevel 1 goto :eof

"%VENV_PY%" track_prices.py >> "%~dp0task_run.log" 2>&1
