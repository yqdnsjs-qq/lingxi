@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   宇之语·灵犀  安装 / 升级
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\upgrade.ps1" %*
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
  echo [完成] 详细过程见 installer.log
) else (
  echo [失败] 返回码 %RC%，详细过程见 installer.log
)
echo.
pause
exit /b %RC%
