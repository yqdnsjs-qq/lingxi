@echo off
chcp 936 >nul
title ”Ó÷Æ”Ô°§¡Èœ¨ - Backup
cd /d "%~dp0"

rem ============================================================
rem  Backup / restore the search index.
rem
rem  Only two things matter (a few MB in total):
rem      data\index\index.db   the index (folder settings live here too)
rem      config.ini            global settings
rem  No need to back up models\ or .cache\ (several GB, re-downloadable)
rem  or venv\ (dependencies).
rem
rem  Chinese text lives in app\backup.py on purpose: this machine runs
rem  codepage 936, and UTF-8 Chinese inside a .bat can make cmd.exe
rem  misparse the file.
rem
rem  Usage:
rem      backup.bat                 make a backup now
rem      backup.bat list            list existing backups
rem      backup.bat restore <file>  restore from a backup
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] venv not found. Run setup.bat first.
  pause
  exit /b 1
)

"%PY%" "%ROOT%\app\backup.py" %*
echo.
pause
