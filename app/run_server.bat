@echo off
rem ============================================================
rem  内部脚本：在独立窗口中真正运行服务
rem  由 start.bat 调用，一般不需要直接双击
rem ============================================================
chcp 936 >nul
title 宇之语·灵犀 服务窗口
cd /d "%~dp0.."

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "HF_HOME=%ROOT%\.cache\huggingface"
set "HF_HUB_CACHE=%HF_HOME%\hub"
set "HUGGINGFACE_HUB_CACHE=%HF_HOME%\hub"
set "TORCH_HOME=%ROOT%\.cache\torch"
set "TMP=%ROOT%\.cache\tmp"
set "TEMP=%ROOT%\.cache\tmp"
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
rem ---- enforce login (multi-user): no login = only the login page ----
set "EG2_ENFORCE_LOGIN=1"

rem ---- 选一个可写的日志文件 ----
rem 上一个没关干净的窗口会占住 server.log，那样 ">>" 直接失败，
rem cmd 立刻退出、服务根本起不来（而且界面上什么错都看不到）。
rem 所以这里先探一下，占了就依次换名字。
set "LOG=%ROOT%\logs\server.log"
2>nul ( >>"%LOG%" echo. ) || set "LOG=%ROOT%\logs\server2.log"
2>nul ( >>"%LOG%" echo. ) || set "LOG=%ROOT%\logs\server3.log"
2>nul ( >>"%LOG%" echo. ) || set "LOG=%ROOT%\logs\server-%RANDOM%.log"

echo.%DATE% %TIME% ==================== 服务启动 ====================>> "%LOG%"
echo.日志文件：%LOG%
echo   服务在后台运行中（运行日志写在上面那个文件里），这个黑框可以最小化，不用管它。
echo   要停止服务请双击 stop.bat。
"%ROOT%\venv\Scripts\python.exe" "%ROOT%\app\server.py" >> "%LOG%" 2>&1
exit /b %errorlevel%
