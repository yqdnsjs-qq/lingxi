@echo off
chcp 936 >nul
title 宇之语·灵犀 - 安装
cd /d "%~dp0"

rem ============================================================
rem  宇之语·灵犀 一键安装
rem  所有组件（Python 环境 / 模型 / 缓存 / 临时文件）都放在本文件夹内，
rem  不写系统目录、不改注册表、不改系统 PATH。
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV=%ROOT%\venv"
set "PY=%VENV%\Scripts\python.exe"

rem ---- 把缓存和临时目录全部重定向到本文件夹（仅对本窗口有效）----
set "HF_HOME=%ROOT%\.cache\huggingface"
set "HF_HUB_CACHE=%HF_HOME%\hub"
set "HUGGINGFACE_HUB_CACHE=%HF_HOME%\hub"
set "TORCH_HOME=%ROOT%\.cache\torch"
set "PIP_CACHE_DIR=%ROOT%\.cache\pip"
set "TMP=%ROOT%\.cache\tmp"
set "TEMP=%ROOT%\.cache\tmp"
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HUB_DISABLE_XET=1"

echo ============================================================
echo   宇之语·灵犀 本地部署 - 安装程序
echo ============================================================
echo   安装位置：%ROOT%
echo   模型仓库：google/embeddinggemma-2
echo.
echo   提示：需要下载约 2-4 GB（PyTorch + 模型），请保持网络畅通。
echo.

for %%D in ("%ROOT%\logs" "%ROOT%\run" "%ROOT%\data\tmp" "%ROOT%\models" "%ROOT%\.cache\tmp" "%ROOT%\.cache\pip" "%ROOT%\.cache\torch" "%ROOT%\.cache\huggingface\hub") do (
  if not exist "%%~D" md "%%~D" >nul 2>nul
)

rem ---------------- 1. 找 Python ----------------
echo [1/5] 检查 Python…
set "PYBASE="
where python >nul 2>nul && set "PYBASE=python"
if not defined PYBASE (
  where py >nul 2>nul && set "PYBASE=py -3"
)
if not defined PYBASE (
  echo.
  echo [错误] 没有找到 Python。请先安装 Python 3.10 - 3.12：
  echo        https://www.python.org/downloads/
  echo        安装时记得勾选 "Add python.exe to PATH"。
  goto :fail
)
%PYBASE% -c "import sys; sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [错误] Python 版本过低，需要 3.10 或更高版本。
  %PYBASE% --version
  goto :fail
)
for /f "delims=" %%V in ('%PYBASE% -c "import sys;print(sys.version.split()[0])"') do echo       已找到 Python %%V

rem ---------------- 读取可选的 pip 代理（config.ini 的 [download] proxy）----------------
set "PIPPROXY="
if exist "%ROOT%\app\info.py" (
  for /f "tokens=1,2 delims==" %%A in ('%PYBASE% "%ROOT%\app\info.py" 2^>nul') do (
    if /i "%%A"=="proxy" if not "%%B"=="" set "PIPPROXY=--proxy %%B"
  )
)
if defined PIPPROXY (
  echo       使用下载代理：%PIPPROXY%
) else (
  rem 没有配置代理时，显式禁用 Windows 系统代理。
  rem 系统代理会把直连拖慢十几倍，而且对境外大文件基本没用。
  set "NO_PROXY=*"
  set "no_proxy=*"
  echo       未配置代理，已禁用系统代理（直连）。
)

rem ---------------- 2. 创建虚拟环境 ----------------
echo.
echo [2/5] 创建独立运行环境 venv …
set "NEED_VENV=1"
if exist "%PY%" (
  "%PY%" -m pip --version >nul 2>nul
  if not errorlevel 1 set "NEED_VENV=0"
)
if "%NEED_VENV%"=="1" (
  if exist "%VENV%" (
    echo       检测到已有环境不完整，正在重建…
    rd /s /q "%VENV%"
  ) else (
    echo       正在创建…
  )
  %PYBASE% -m venv "%VENV%"
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败，请确认已安装 Python 3.10 以上版本。
    goto :fail
  )
  "%PY%" -m pip --version >nul 2>nul
  if errorlevel 1 (
    echo       正在初始化 pip…
    "%PY%" -m ensurepip --upgrade --default-pip
  )
  "%PY%" -m pip --version >nul 2>nul
  if errorlevel 1 (
    echo [错误] 虚拟环境中的 pip 不可用。
    goto :fail
  )
) else (
  echo       已存在，跳过。
)
"%PY%" -m pip install --upgrade pip >nul 2>nul
echo       完成。

rem ---------------- 3. 安装 PyTorch ----------------
echo.
echo [3/5] 安装 PyTorch（显卡加速版，体积较大，请耐心等待）…
call :install_pytorch
if errorlevel 1 (
  echo [错误] PyTorch 安装失败。
  goto :fail
)

rem ---------------- 4. 安装其余依赖 ----------------
echo.
echo [4/5] 安装 transformers / sentence-transformers / fastapi 等依赖…
call :install_deps
if errorlevel 1 (
  echo [错误] 依赖安装失败。
  goto :fail
)

rem ---------------- 5. 下载模型 ----------------
echo.
echo [5/5] 下载模型 google/embeddinggemma-2（约 1.5 GB）…
call :download_model
if errorlevel 1 (
  echo [错误] 模型下载失败。
  goto :fail
)

rem ---------------- 验证 ----------------
echo.
echo 正在验证安装结果…
"%PY%" -c "import torch, transformers, sentence_transformers, fastapi, PIL; print('  PyTorch      :', torch.__version__); print('  CUDA 可用    :', torch.cuda.is_available()); print('  GPU          :', (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU 模式')); print('  Transformers :', transformers.__version__); print('  SentenceTrf  :', sentence_transformers.__version__)"
if errorlevel 1 (
  echo [错误] 验证未通过。
  goto :fail
)

echo.
echo ============================================================
echo   安装完成！
echo   双击 start.bat 启动服务，双击 stop.bat 停止服务。
echo ============================================================
echo.
pause
exit /b 0

rem ============================================================
rem  子过程
rem ============================================================
:install_pytorch
"%PY%" -c "import torch" >nul 2>nul
if not errorlevel 1 (
  echo       已安装，跳过。
  exit /b 0
)
for %%U in (cu130 cu128 cpu) do (
  echo       尝试 %%U 版本（pip 直接下载）…
  "%PY%" -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/%%U %PIPPROXY% --retries 15 --timeout 180
  if not errorlevel 1 exit /b 0
  echo       下载中断，改用断点续传模式重试（已下载的部分会保留）…
  "%PY%" "%ROOT%\app\fetch_torch.py" --indexes %%U --connections 4 %PIPPROXY%
  if not errorlevel 1 exit /b 0
  echo       %%U 版本安装失败，换下一个源 …
)
exit /b 1

:install_deps
"%PY%" -m pip install -r "%ROOT%\requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple --retries 15 --timeout 180 %PIPPROXY%
if not errorlevel 1 exit /b 0
echo       清华镜像失败，改用官方 PyPI 重试 …
"%PY%" -m pip install -r "%ROOT%\requirements.txt" --retries 15 --timeout 180 %PIPPROXY%
exit /b %errorlevel%

:download_model
"%PY%" "%ROOT%\app\download_model.py"
exit /b %errorlevel%

:fail
echo.
echo ============================================================
echo   安装未完成，请根据上面的提示处理后重新运行 setup.bat
echo ============================================================
echo.
pause
exit /b 1
