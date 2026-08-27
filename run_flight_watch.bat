@echo off
rem ---------------------------------------------------------------------
rem  Keep ALL text in this file ASCII-only (see _ensure_venv.bat comment
rem  for why: cmd.exe re-reads batch files by byte offset while running).
rem ---------------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

call "%~dp0..\_ensure_venv.bat"
if errorlevel 1 goto :eof

"%VENV_PY%" track_prices.py
