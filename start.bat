@echo off
chcp 936 >nul
rem 1 = 由 启动灵犀.vbs 隐藏启动（不能用真 pause）
if not defined EG2_HIDDEN set "EG2_HIDDEN=0"
title 宇之语·灵犀 - 启动
cd /d "%~dp0"

rem 如果装过 OCR 组件（install_ocr.bat），加载它的路径设置
if exist "%~dp0ocr-env.bat" call "%~dp0ocr-env.bat"

rem ============================================================
rem  宇之语·灵犀 启动脚本
rem  1. 检查运行环境与模型
rem  2. 在后台（最小化窗口）启动本地服务
rem  3. 等待模型加载完成后自动打开网页
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\venv\Scripts\python.exe"
rem 可选参数：要打开的页面
rem   不带参数 -> 文件搜索页（日常用这个）
rem   embed    -> 嵌入模型测试页（原版界面）
rem   docs     -> 接口文档
set "PAGE=%~1"
if not defined PAGE set "PAGE=search"
if /i "%PAGE%"=="embed" set "PAGE="

set "HF_HOME=%ROOT%\.cache\huggingface"
set "HF_HUB_CACHE=%HF_HOME%\hub"
set "HUGGINGFACE_HUB_CACHE=%HF_HOME%\hub"
set "TORCH_HOME=%ROOT%\.cache\torch"
set "TMP=%ROOT%\.cache\tmp"
set "TEMP=%ROOT%\.cache\tmp"
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"

echo ============================================================
echo   宇之语·灵犀 本地服务 - 启动
echo ============================================================
echo.

if not exist "%PY%" (
  echo [错误] 还没有安装运行环境。
  echo        请先双击运行 setup.bat 完成安装。
  echo.
call :pause_or_wait
  exit /b 1
)

rem ---- 读取配置 ----
for /f "tokens=1,2 delims==" %%A in ('""%PY%" "%ROOT%\app\info.py""') do (
  if /i "%%A"=="host"        set "HOST=%%B"
  if /i "%%A"=="check_host"  set "CHOST=%%B"
  if /i "%%A"=="port"        set "PORT=%%B"
  if /i "%%A"=="open_browser" set "OPENBROWSER=%%B"
  if /i "%%A"=="model_dir"   set "MODELDIR=%%B"
)
if not defined HOST set "HOST=127.0.0.1"
if not defined CHOST set "CHOST=127.0.0.1"
if not defined PORT set "PORT=8199"
if not defined OPENBROWSER set "OPENBROWSER=yes"
if not defined MODELDIR set "MODELDIR=%ROOT%\models\embeddinggemma-2"

set "URL=http://%CHOST%:%PORT%/"

if not exist "%MODELDIR%\config.json" (
  echo [错误] 模型文件不存在：%MODELDIR%
  echo        请先双击运行 setup.bat 下载模型。
  echo.
call :pause_or_wait
  exit /b 1
)

rem ---- 是否已经在运行 ----
rem 注意顺序：先查健康，再清窗口。
rem 以前是先清窗口，结果「服务本来好好开着」也会被 taskkill 掉；而那种强杀
rem 不会清理 run\server.pid，留下过期 pid，紧接着的启动等待就会拿旧 pid 去
rem tasklist 查不到 → 误判「服务已死」→ 直接跳超时分支，界面根本不打开。
call :is_healthy
if not errorlevel 1 (
  echo [提示] 服务已经在运行：%URL%
  echo        直接打开网页。
  if /i "%OPENBROWSER%"=="yes" call :open_page
goto :after_open

:open_page
rem 界面窗口已经开着 -> 只把它调到前面，不要关掉重开。
rem （2026-10-17 项目主反馈：以前每次运行 start.bat 都会把正在看的界面关掉重开）
if exist "%PY%" "%PY%" "%ROOT%\app\close_ui.py" --reuse
if not errorlevel 1 goto :after_open
rem 没有开着的界面窗口 -> 清掉残留的再开新的（防越积越多）。
if exist "%PY%" "%PY%" "%ROOT%\app\close_ui.py" --wait 2
rem 优先用 Chrome/Edge 的 --app 模式：独立窗口、无地址栏，
rem 用专门的 profile（.browser），不影响用户其它浏览器标签。
set "BROWSER="
for %%P in (
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
) do if not defined BROWSER if exist %%P set "BROWSER=%%~P"
if defined BROWSER (
  start "" "%BROWSER%" --app="%URL%%PAGE%" --user-data-dir="%ROOT%\.browser" --window-size=1500,950 --no-first-run --no-default-browser-check
) else (
  start "" "%URL%%PAGE%"
)
exit /b

:after_open
  >nul ping -n 3 127.0.0.1
  exit /b 0
)

rem ---- 服务没在跑：清掉遗留的服务窗口，并删掉过期的 pid 记录 ----
rem pid 必须删：server.py 要等模型加载完才写它，中间这段时间文件里是上一次的旧 pid，
rem 不删的话下面的等待循环会误判「服务已死」而提前放弃。
call :kill_service_windows
if exist "%ROOT%\run\server.pid" del "%ROOT%\run\server.pid" >nul 2>nul

rem ---- 启动 ----
echo 正在启动服务（模型加载需要 10-60 秒，请稍候）…
echo 服务窗口已最小化，运行日志：%ROOT%\logs\server.log
echo.
rem ★ 2026-11-02 项目主：这个黑框太碍眼、还容易误关 → 改成隐藏启动
wscript //nologo "%ROOT%\app\run_server_hidden.vbs"

set /a TRIES=0
:waitloop
>nul ping -n 2 127.0.0.1
call :is_healthy
if not errorlevel 1 goto :ready
set /a TRIES+=1
if %TRIES% geq 150 goto :timeout
call :still_alive
if errorlevel 1 goto :timeout
goto :waitloop

:ready
echo ============================================================
echo   服务启动成功！
echo.
echo   文件搜索页   : %URL%search   （本次已打开）
echo   嵌入测试界面 : %URL%        （start.bat embed）
echo   接口地址     : %URL%v1/embeddings  （OpenAI 兼容）
echo   接口文档     : %URL%docs
echo   关闭         : 直接关掉这个界面窗口（服务会自动停）
echo                   也可以双击 stop.bat
echo ============================================================
echo.
if /i "%OPENBROWSER%"=="yes" call :open_page
>nul ping -n 4 127.0.0.1
exit /b 0

:timeout
echo.
echo [错误] 服务启动失败或超时。
echo        下面是日志最后 25 行：
echo ------------------------------------------------------------
powershell -NoProfile -Command "if (Test-Path '%ROOT%\logs\server.log') { Get-Content -Tail 25 -Encoding UTF8 '%ROOT%\logs\server.log' }"
echo ------------------------------------------------------------
echo 完整日志：%ROOT%\logs\server.log
echo.
call :pause_or_wait
exit /b 1

rem ============================================================
rem  子过程
rem ============================================================
:is_healthy
curl.exe -s -o nul --max-time 3 "%URL%health" >nul 2>nul
if not errorlevel 1 exit /b 0
if not exist "%PY%" exit /b 1
"%PY%" -c "import urllib.request,sys;urllib.request.urlopen('%URL%health',timeout=3);sys.exit(0)" >nul 2>nul
exit /b %errorlevel%

rem 返回 0 = 继续等；返回 1 = 判定服务已经死了、别等了。
rem 注意：没有 pid 文件时返回 0（继续等）—— 因为 pid 文件是模型加载完才写的，
rem 那之前「没有 pid 文件」是正常状态，不能当成服务已死。
:still_alive
if not exist "%ROOT%\run\server.pid" exit /b 0
set "SRVPID="
set /p SRVPID=<"%ROOT%\run\server.pid"
if not defined SRVPID exit /b 0
tasklist /fi "PID eq %SRVPID%" /nh 2>nul | findstr /i "python" >nul
exit /b %errorlevel%

rem 按窗口标题结束上一次留下的服务窗口。
rem taskkill 自带 WINDOWTITLE 过滤器，比「tasklist 解析出 PID 再杀」可靠
rem （tasklist /v 在有些机器上会因权限失败）。
:kill_service_windows
for %%T in ("宇之语·灵犀 服务窗口" "宇之语·灵犀 服务" "EmbeddingGemma 2 服务窗口") do taskkill /f /fi "WINDOWTITLE eq %%~T" >nul 2>nul
exit /b 0
rem ---- 隐藏模式下不能真 pause：窗口看不见，会一直挂着 ----
:pause_or_wait
if "%EG2_HIDDEN%"=="1" (
  >nul ping -n 9 127.0.0.1
  exit /b 0
)
pause
exit /b 0
