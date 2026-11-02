@echo off
chcp 936 >nul
title 宇之语·灵犀 - 状态
cd /d "%~dp0"

rem ============================================================
rem  查看服务是否在运行、用的什么设备，以及桌面功能能不能用
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [错误] 还没有安装运行环境，请先双击 setup.bat。
  pause
  exit /b 1
)

"%PY%" "%ROOT%\app\status.py"
echo.
pause
