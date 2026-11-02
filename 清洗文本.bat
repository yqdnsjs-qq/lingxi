@echo off
chcp 936 >nul
title 文本清洗 v0.2（双击运行）
cd /d "%~dp0"

rem ============================================================
rem  双击这个文件 -> 弹框选文件/文件夹 -> 选处理方式 -> 清洗
rem  真正的清洗逻辑是同目录的 clean_text.py（独立工具，可单独拷走）
rem  注意：这个文件是 GBK 编码，改它别用会转成 UTF-8 的编辑器
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "TOOL=%ROOT%\clean_text.py"
set "PICK=%ROOT%\run\pick_files.ps1"
set "EXE=%ROOT%\clean_text.exe"

rem 单文件 exe 启动时要把自己解压到临时目录：有些机器 TEMP 不可写（会报
rem "Could not create temporary directory"），所以指到工具目录下，不依赖系统环境
set "TMP=%ROOT%\.tmp"
set "TEMP=%ROOT%\.tmp"
if not exist "%TMP%" mkdir "%TMP%" >nul 2>nul

rem 有 exe 就用 exe（免安装）；没有才退回 python
if not exist "%PY%" set "PY=python"
if exist "%EXE%" (
  set "TOOLRUN="%EXE%""
) else (
  set "TOOLRUN="%PY%" "%TOOL%""
)

echo ============================================================
echo   文本清洗 v0.2（清掉控制字符、零宽字符、孤立代理、URL 噪声行、多余空行）
echo ============================================================
echo.

if not exist "%TOOL%" if not exist "%EXE%" (
  echo [错误] 找不到 clean_text.exe 或 clean_text.py -- 它们应该和这个 bat 在同一目录
  echo.
  pause
  exit /b 1
)
if not exist "%PICK%" (
  echo [错误] 找不到 run\pick_files.ps1
  echo.
  pause
  exit /b 1
)

set "MODE=files"
choice /c 12 /n /m "要处理什么？  1=选文件（可多选）  2=选文件夹 "
if errorlevel 2 set "MODE=folder"

echo.
echo 正在打开选择框……
set "PICKED="
for /f "usebackq delims=" %%A in (`powershell -NoProfile -STA -ExecutionPolicy Bypass -File "%PICK%" -Mode %MODE%`) do set "PICKED=%%A"

if not defined PICKED (
  echo 没有选择任何东西，退出。
  echo.
  pause
  exit /b 0
)
echo.
echo 已选择：%PICKED%
echo.

choice /c 123 /n /m "怎么处理？  1=只预览（不改文件）  2=写到新目录  3=原地改（自动备份 .bak） "
if errorlevel 3 goto :inplace
if errorlevel 2 goto :toout
goto :preview

:preview
echo.
%TOOLRUN% %PICKED:|= %
echo.
pause
exit /b 0

:toout
set "OUT=%ROOT%\清洗结果"
echo.
echo 输出目录：%OUT%（重名的会覆盖）
"%PY%" "%TOOL%" %PICKED:|= % --out "%OUT%"
echo.
if exist "%OUT%" start "" "%OUT%"
pause
exit /b 0

:inplace
echo.
echo 【注意】原地改：每个文件会先备份成 xxx.bak，且不会覆盖更早的备份
choice /c 12 /n /m "确定要原地修改吗？  1=是  2=不改了 "
if errorlevel 2 exit /b 0
"%PY%" "%TOOL%" %PICKED:|= % --inplace
echo.
pause
exit /b 0
