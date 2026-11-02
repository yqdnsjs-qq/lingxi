@echo off
chcp 936 >nul
title 宇之语·灵犀 - 停止
cd /d "%~dp0"

rem ============================================================
rem  宇之语·灵犀 停止脚本
rem  1. 先请求服务优雅退出（等待 15 秒）
rem  2. 没退出就强制结束进程
rem  3. 兜底：按端口清理残留进程
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "PIDFILE=%ROOT%\run\server.pid"

echo ============================================================
echo   宇之语·灵犀 本地服务 - 停止
echo ============================================================
echo.

if not exist "%PY%" (
  set "PORT=8199"
  set "CHOST=127.0.0.1"
) else (
  for /f "tokens=1,2 delims==" %%A in ('""%PY%" "%ROOT%\app\info.py""') do (
    if /i "%%A"=="port"       set "PORT=%%B"
    if /i "%%A"=="check_host" set "CHOST=%%B"
  )
)
if not defined CHOST set "CHOST=127.0.0.1"
if not defined PORT set "PORT=8199"

set "SRVPID="
if exist "%PIDFILE%" (
  set /p SRVPID=<"%PIDFILE%"
)

if not defined SRVPID (
  echo [提示] 没有找到运行中的服务记录，尝试按端口 %PORT% 检查…
  call :kill_by_port
  goto :done
)

echo [1/3] 请求服务优雅退出（PID %SRVPID%）…
curl.exe -s -o nul --max-time 5 -X POST "http://%CHOST%:%PORT%/admin/shutdown" >nul 2>nul
if errorlevel 1 (
  if exist "%PY%" "%PY%" -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://%CHOST%:%PORT%/admin/shutdown',method='POST'),timeout=5)" >nul 2>nul
)

set /a WAIT=0
:waitloop
>nul ping -n 2 127.0.0.1
tasklist /fi "PID eq %SRVPID%" /nh 2>nul | findstr /i "python" >nul
if errorlevel 1 goto :stopped
set /a WAIT+=1
if %WAIT% geq 15 goto :force
goto :waitloop

:force
echo [2/3] 服务没有响应，强制结束进程 %SRVPID% …
call :kill_one %SRVPID%
>nul ping -n 3 127.0.0.1
goto :done

:stopped
echo [2/3] 服务已正常退出。

:done
call :kill_by_port
rem ---- 关掉界面窗口（start.bat 用浏览器 --app 模式打开的那些独立窗口）----
rem 以前这里是「按窗口标题 taskkill」，而且整段落在 exit /b 0 后面成了死代码，
rem 从来没执行过 —— 所以每启动一次就多留一个界面窗口，越积越多。
rem 现在改成调 app\close_ui.py 枚举窗口：先发 WM_CLOSE 让它自己关
rem （Chrome 有机会把 localStorage 落盘，界面设置就不容易丢），关不掉再强杀。
call :close_ui_windows
call :kill_service_windows
if exist "%PIDFILE%" del "%PIDFILE%" >nul 2>nul
echo [3/3] 清理完成。
echo.
echo 服务已停止，可以安全关闭本窗口。
>nul ping -n 4 127.0.0.1
exit /b 0

rem 关界面窗口：优先用 python 精确关，找不到 python 才退回按标题 taskkill。
:close_ui_windows
if exist "%PY%" "%PY%" "%ROOT%\app\close_ui.py" --wait 3
taskkill /f /fi "WINDOWTITLE eq 宇之语 · 灵犀 · 本地文件与知识中枢" >nul 2>nul
taskkill /f /fi "WINDOWTITLE eq 宇之语 · 灵犀 · 本地文件与知识中枢 " >nul 2>nul
exit /b 0

rem 结束上一次留下的服务窗口（那个最小化的黑框）。
rem taskkill 自带 WINDOWTITLE 过滤器，比「tasklist 解析出 PID 再杀」可靠
rem （tasklist /v 在有些机器上会因权限失败）。
:kill_service_windows
for %%T in ("宇之语·灵犀 服务窗口" "宇之语·灵犀 服务" "EmbeddingGemma 2 服务窗口") do taskkill /f /fi "WINDOWTITLE eq %%~T" >nul 2>nul
exit /b 0

rem ============================================================
:kill_by_port
set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
  echo       结束占用端口 %PORT% 的进程 PID=%%P
  call :kill_one %%P
  set "FOUND=1"
)
if not defined FOUND echo       端口 %PORT% 上没有残留进程。
exit /b 0

rem 先试 taskkill；某些情况下（权限/安全软件）它会失败，再用 PowerShell 兜底
:kill_one
taskkill /PID %1 /T /F >nul 2>nul
tasklist /fi "PID eq %1" /nh 2>nul | findstr /r "[0-9]" >nul
if errorlevel 1 exit /b 0
echo       taskkill 未成功，改用 PowerShell 结束…
powershell -NoProfile -Command "Stop-Process -Id %1 -Force -ErrorAction SilentlyContinue" >nul 2>nul
exit /b 0
