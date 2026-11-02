@echo off
chcp 936 >nul
title 宇之语·灵犀 · 从仓库更新
cd /d "%~dp0.."

echo ============================================================
echo   从 GitHub 拉取最新代码并重启（数据、配置不会被改动）
echo ============================================================
echo.

where git >nul 2>nul
if errorlevel 1 (
  echo [!] 没找到 git，请先安装 Git for Windows 并加进 PATH
  pause
  exit /b 1
)

echo [1/4] 停服务 ...
call stop.bat >nul 2>nul

echo [2/4] 拉取最新代码 ...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo [!] 拉取失败。常见原因：
  echo     1^) 本地有改动没提交 —— 先跑 git status 看看
  echo     2^) 没配 remote —— 先 git remote add origin 你的仓库地址
  echo     3^) 网络/代理问题
  echo     回滚办法：本目录下 git checkout -- . 丢弃本地改动后再试
  pause
  exit /b 1
)

echo [3/4] 重新生成前端静态文件哈希 ...
if exist venv\Scripts\python.exe (
  venv\Scripts\python.exe run\rehash_static.py
) else (
  echo     [跳过] 还没建 venv，先跑 setup.bat
)

echo [4/4] 启动 ...
call start.bat

echo.
echo 完成。浏览器请按 Ctrl+F5 硬刷新；右上角版本号应变成最新版本。
pause
