@echo off
chcp 936 >nul
title ”Ó÷Æ”Ô°§¡Èœ¨ - Restore
cd /d "%~dp0"

rem ============================================================
rem  Restore the search index from a backup.
rem
rem  Double-click this file: a Windows file-picker opens (pointing at
rem  the backups\ folder). Pick the .db, confirm, and it does the rest:
rem    - stops the service if it is running
rem    - keeps the current index as index-before-restore-<time>.db
rem    - offers to start the service again when finished
rem
rem  Command line (for scripts):  restore.bat index-20260101-120000.db
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] venv not found. Run setup.bat first.
  pause
  exit /b 1
)

"%PY%" "%ROOT%\app\backup.py" restore %*
echo.
pause
