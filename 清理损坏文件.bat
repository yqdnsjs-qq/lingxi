@echo off
chcp 936 >nul
title 宇之语·灵犀 - 清理损坏文件
cd /d "%~dp0"

rem ============================================================
rem  清理索引扫描发现的损坏文件
rem  默认移到「回收站」，可恢复，不是永久删除
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PS1=%ROOT%\app\clean_broken.ps1"

if not exist "%PS1%" (
  echo [错误] 找不到 %PS1%
  pause
  exit /b 1
)

:MENU
cls
echo ============================================================
echo   清理损坏文件
echo ============================================================
echo.
echo   这些文件是索引扫描时发现的打不开的文件。
echo   清理方式：移到「回收站」---- 后悔了可以恢复。
echo.
echo   [1] 只看清单（不删除）              ^<- 建议先看这个
echo   [2] 只清网页残图（零风险）          ^<- D:\notes\obsidian 下的
echo   [3] 清「有内容但损坏」的            ^<- 保留 0 字节的，最常用
echo   [4] 清全部（含 0 字节的）           ^<- 0 字节可能是同步没完成，慎用
echo   [5] 退出
echo.
set "CH="
set /p CH=  请选择 [1-5]: 

if "%CH%"=="1" goto :LIST
if "%CH%"=="2" goto :OBS
if "%CH%"=="3" goto :CONTENT
if "%CH%"=="4" goto :ALL
if "%CH%"=="5" exit /b 0
goto :MENU

:LIST
cls
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Mode list
echo.
echo   清单文件：
echo     %ROOT%\损坏文件清单.txt
echo     %ROOT%\损坏文件清单.csv
pause
goto :MENU

:OBS
cls
echo.
echo  [零风险] 只清网页剪藏里没下全的残图（不是你的照片）
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Mode obsidian
pause
goto :MENU

:CONTENT
cls
echo.
echo  [常用] 清「有内容但损坏」的，保留 0 字节的
echo         0 字节的那批可能是 NAS 同步没完成，需要你自己确认后再处理
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Mode content
pause
goto :MENU

:ALL
cls
echo.
echo  ============================================================
echo   [慎用] 这会连「0 字节」的一起清理
echo  ============================================================
echo.
echo   0 字节的 10 个文件在 \\NAS\Shared\健达\生产记录 下，
echo   它们可能是同步中断的残留 ---- 删之前请确认原图在别处还有。
echo.
set "CF="
set /p CF=  确认要清全部吗？输入 YES 继续: 
if /i not "%CF%"=="YES" goto :MENU
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Mode all
pause
goto :MENU
