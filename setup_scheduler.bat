@echo off
rem ---------------------------------------------------------------------
rem  Keep ALL text in this file ASCII-only (cmd.exe re-reads batch files
rem  by byte offset while running -- non-ASCII shifts offsets and breaks
rem  mid-run; see _ensure_venv.bat).
rem
rem  Registers the flight_watch scheduled tasks on THIS PC.
rem  Run this once on each computer you use (Task Scheduler entries are
rem  per-machine and are NOT synced by OneDrive).
rem
rem  Double-click, or run from a terminal. No admin rights needed -- the
rem  tasks run as the current logged-in user, only while logged on.
rem ---------------------------------------------------------------------
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_scheduler.ps1" %*
echo.
pause
