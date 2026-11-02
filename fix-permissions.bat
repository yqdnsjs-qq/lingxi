@echo off
chcp 936 >nul
title ÓîÖ®Óï¡¤ÁéÏ¬ - Fix Permissions
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem ============================================================
rem  Repair the "Low integrity" label on the project folder.
rem
rem  Why: any executable file labeled Low forces its process to run at
rem  Low integrity, no matter who launches it. Low integrity processes
rem  are not allowed to drive Explorer, so "locate file" fails with
rem  0xc0000142 / 0x80070005.
rem
rem  We only relabel the project ROOT (one call, no /T): Windows then
rem  propagates the new label to every child through inheritance.
rem  Needs an elevated console (SeRelabelPrivilege).
rem ============================================================

net session >nul 2>nul
if errorlevel 1 (
  echo Requesting administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)

echo ============================================================
echo   ÓîÖ®Óï¡¤ÁéÏ¬ - Fix Permissions
echo ============================================================
echo   Target: %ROOT%
echo.
echo   [Before] project root:
icacls "%ROOT%" 2>nul | findstr /i "Mandatory"
echo   [Before] python.exe:
icacls "%ROOT%\venv\Scripts\python.exe" 2>nul | findstr /i "Mandatory"
echo.
echo   [1/3] Setting project root to Medium (inheritance updates children)...
icacls "%ROOT%" /setintegritylevel (OI)(CI)Medium
echo.
echo   [2/3] Checking python.exe...
icacls "%ROOT%\venv\Scripts\python.exe" 2>nul | findstr /i "Low Mandatory" >nul
if not errorlevel 1 (
  echo         still Low - running a full recursive fix, this can take minutes...
  icacls "%ROOT%\venv" /setintegritylevel (OI)(CI)Medium /T /C /Q
  echo         done
) else (
  echo         ok, python.exe is no longer Low
)
echo.
echo   [3/3] Removing the stray "deny delete" rule...
icacls "%ROOT%" /remove:d Everyone /T /C /Q >nul 2>nul
echo         done
echo.
echo   [After] project root:
icacls "%ROOT%" 2>nul | findstr /i "Mandatory"
echo   [After] python.exe:
icacls "%ROOT%\venv\Scripts\python.exe" 2>nul | findstr /i "Mandatory"
echo.
echo ============================================================
echo   Done. Next steps are printed below.
echo ============================================================
echo.
"%ROOT%\venv\Scripts\python.exe" "%ROOT%\app\fix_integrity.py" 2>nul
pause
exit /b 0
