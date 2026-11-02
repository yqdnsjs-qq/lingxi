@echo off
chcp 936 >nul
title 宇之语·灵犀 - 安装 PDF 原版式支持
cd /d "%~dp0"

rem ============================================================
rem  装 PyMuPDF：PDF 预览就能把某一页渲染成图片，看得到表格版式。
rem  为什么要单独一个脚本：venv 被 fix-permissions.bat 设成了 Medium
rem  完整性级别（防止低权限进程改解释器），所以装包必须在你自己
rem  的正常权限下做。优先用项目里已经下好的 wheel（离线、快）。
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "PIP_CACHE_DIR=%ROOT%\.cache\pip"
set "TMP=%ROOT%\.cache\tmp"
set "TEMP=%ROOT%\.cache\tmp"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo ============================================================
echo   宇之语·灵犀 - 安装 PDF 原版式支持（PyMuPDF）
echo ============================================================
echo.

if not exist "%PY%" (
  echo [错误] 没找到运行环境，请先双击 setup.bat。
  echo.
  pause
  exit /b 1
)

echo [1/3] 检查是否已经装过…
"%PY%" -c "import pymupdf,sys;print('已经装好了：PyMuPDF',pymupdf.__version__);sys.exit(9)" 2>nul
if errorlevel 9 goto :done

echo [2/3] 安装 PyMuPDF…
if exist "%ROOT%\.cache\wheels\pymupdf-*.whl" (
  echo       用项目里已下好的 wheel（离线安装）
  "%PY%" -m pip install --no-index --find-links "%ROOT%\.cache\wheels" pymupdf
) else (
  echo       本地没有 wheel，改从清华镜像下载
  "%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pymupdf
)
if errorlevel 1 goto :fail

echo [3/3] 验证…
"%PY%" -c "import pymupdf;print('安装成功：PyMuPDF',pymupdf.__version__)"
if errorlevel 1 goto :fail

echo.
echo 装好了。回到界面打开一个 PDF，就能用「看原版式」看表格样式。
echo 不用重新索引：页面是预览时现渲染的。
echo.
pause
exit /b 0

:fail
echo.
echo [失败] 没有装上。可以手动试一次：
echo        "%PY%" -m pip install pymupdf
echo.
pause
exit /b 1

:done
echo       已经装过了，不用重复安装。
echo.
pause
exit /b 0
