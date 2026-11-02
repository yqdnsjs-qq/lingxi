@echo off
chcp 936 >nul
title 宇之语·灵犀 - 安装 OCR 组件（可选）
cd /d "%~dp0"

rem ============================================================
rem  OCR 是**可选组件**：不装也能用灵犀，只是 PDF / 图片不提文字。
rem
rem  本脚本装的是 **RapidOCR**（最轻，纯 CPU，约 260 MB）：
rem    · 不占显存，不会和嵌入模型抢 GPU
rem    · 扫描件、截图、PDF 里的图片都能提字
rem    · 效果够日常用；要更高质量可以另装 PP-OCRv6 或 MinerU（见 README）
rem
rem  装完它会自动写好环境变量，灵犀启动时就能识别到 ✓
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"
set "OCR_DIR=%ROOT%\ocr-rapid"
set "OCR_PY=%OCR_DIR%\.venv\Scripts\python.exe"

echo.
echo  ============================================================
echo   安装 OCR 组件（RapidOCR，约 260 MB）
echo  ============================================================
echo.

if not exist "%PY%" (
  echo  [错误] 没找到 venv\Scripts\python.exe
  echo         请先跑一遍 setup.bat
  echo.
  pause
  exit /b 1
)

echo  [1/4] 创建独立环境（不和主环境混，避免依赖冲突）
echo        位置：%OCR_DIR%
if exist "%OCR_PY%" (
  echo        已存在，跳过
) else (
  if not exist "%OCR_DIR%" mkdir "%OCR_DIR%"
  "%PY%" -m venv "%OCR_DIR%\.venv"
  if errorlevel 1 (
    echo  [错误] 创建 venv 失败
    pause & exit /b 1
  )
)

echo.
echo  [2/4] 安装 rapidocr-onnxruntime
"%OCR_PY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%OCR_PY%" -m pip install --disable-pip-version-check rapidocr-onnxruntime
if errorlevel 1 (
  echo.
  echo  [错误] 安装失败。可以换国内源重试：
  echo     "%OCR_PY%" -m pip install rapidocr-onnxruntime -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo.
  pause & exit /b 1
)

echo.
echo  [3/4] 放入桥接脚本
if exist "%ROOT%\run\ocr_bridges\rapid_bridge.py" (
  copy /y "%ROOT%\run\ocr_bridges\rapid_bridge.py" "%OCR_DIR%\bridge.py" >nul
  echo        已复制 bridge.py
) else (
  echo  [警告] 没找到 run\ocr_bridges\rapid_bridge.py
  echo         引擎装好了，但灵犀还调不动它
)

echo.
echo  [4/4] 写入环境变量（只影响本文件夹，不污染系统）
> "%ROOT%\ocr-env.bat" echo @echo off
>> "%ROOT%\ocr-env.bat" echo rem 由 install_ocr.bat 生成：告诉灵犀 RapidOCR 在哪
>> "%ROOT%\ocr-env.bat" echo set "EG2_OCR_RAPID_PY=%OCR_PY%"
>> "%ROOT%\ocr-env.bat" echo set "EG2_OCR_RAPID_BRIDGE=%OCR_DIR%\bridge.py"
echo        已生成 ocr-env.bat

echo.
echo  自检：
"%OCR_PY%" "%OCR_DIR%\bridge.py" 2>nul | findstr /c:"ok" >nul && (
  echo        ✓ RapidOCR 能正常调用
) || (
  echo        ! 自检没输出（没传图片时通常正常，可以先跳过）
)

echo.
echo  ============================================================
echo   完成
echo  ============================================================
echo.
echo  还需要**两步**才算真正生效：
echo.
echo    1) 在 start.bat 里加一行（放在 cd /d "%%~dp0" 之后）：
echo           if exist "ocr-env.bat" call "ocr-env.bat"
echo.
echo       或者每次启动前手动执行一次 ocr-env.bat
echo.
echo    2) 重启灵犀（stop.bat ^> start.bat）
echo.
echo  验证：进「设置 → OCR」，「RapidOCR」那一项应该显示可用 ✓
echo.
pause
