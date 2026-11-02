@echo off
chcp 936 >nul
title 宇之语·灵犀 - 自检
cd /d "%~dp0"

rem ============================================================
rem  自检：确认服务能连上、模型可用、各种输入能正常编码
rem  需要先双击 start.bat 启动服务
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [错误] 还没有安装运行环境，请先双击 setup.bat。
  echo.
  pause
  exit /b 1
)

"%PY%" "%ROOT%\app\selftest.py" %*
echo.
pause
