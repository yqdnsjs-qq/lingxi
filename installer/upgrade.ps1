<#
  宇之语·灵犀 一键安装 / 升级
  ------------------------------------------------------------
  一个脚本干两件事：
    · 目标目录里已经有灵犀（app\version.py 存在） → 就地把旧版升级成新版本
    · 没有                                        → 当作全新安装，铺好文件并提示下一步

  绝对不动的：data\（索引库/知识库分库）、models\、venv\、config.ini、
              logs\、backups\、.browser\  —— 升级只换程序，不碰数据 ✓

  参数：
    -Target <目录>   指定安装目录（不给就自动探测）
    -Force           版本相同也照样升级
    -NoStart         升级完不自动启动
#>
param(
    [string]$Target = "",
    [switch]$Force,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$Pkg = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogFile = Join-Path $Pkg "installer.log"

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { }
}

function Read-Ver([string]$dir) {
    $f = Join-Path $dir "app\version.py"
    if (-not (Test-Path $f)) { return "" }
    $m = Select-String -Path $f -Pattern '^VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return ""
}

function Read-Port([string]$dir) {
    $f = Join-Path $dir "config.ini"
    if (Test-Path $f) {
        $m = Select-String -Path $f -Pattern '^\s*port\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { return [int]$m.Matches[0].Groups[1].Value }
    }
    return 8199
}

function Stop-Service([string]$dir) {
    $port = Read-Port $dir
    Log "停服务：POST 127.0.0.1:$port/admin/shutdown"
    try {
        & curl.exe --noproxy "*" -s -X POST --max-time 10 "http://127.0.0.1:$port/admin/shutdown" | Out-Null
    } catch { }
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        $open = (Test-NetConnection -ComputerName 127.0.0.1 -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue)
        if (-not $open) { Log "端口 $port 已释放"; return }
    }
    Log "⚠ 端口 $port 还开着，按进程命令行兜底杀"
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        if ($p.CommandLine -and $p.CommandLine -like "*$dir*" -and $p.CommandLine -like "*server.py*") {
            Log ("  kill pid {0}" -f $p.ProcessId)
            try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
        }
    }
    Start-Sleep -Seconds 2
}

# ---------------------------------------------------------------- 目标目录
$newVer = Read-Ver $Pkg
if (-not $newVer) { Log "!! 包里没有 app\version.py，这个包不完整"; exit 2 }
Log "安装包版本：$newVer   位置：$Pkg"

if (-not $Target) {
    $cands = @()
    if ($env:LINGXI_HOME) { $cands += $env:LINGXI_HOME }
    $cands += @(
        "D:\GJ\lingxi", "D:\lingxi",
        "D:\GJ\embeddinggemma-2", "D:\lingxi",
        "D:\lingxi", "C:\lingxi"
    )
    foreach ($c in $cands) {
        if ($c -and (Test-Path (Join-Path $c "app\version.py"))) { $Target = $c; break }
    }
    if (-not $Target) { $Target = "D:\GJ\lingxi" }
}
$Target = [System.IO.Path]::GetFullPath($Target)
Log "安装目录：$Target"

$oldVer = Read-Ver $Target
$isUpgrade = [bool]$oldVer
if ($isUpgrade) {
    Log "发现已有安装：v$oldVer"
    if ($oldVer -eq $newVer -and -not $Force) {
        Log "版本已经一样（v$newVer），加 -Force 才会重装。"
        exit 0
    }
} else {
    Log "没有发现旧版 → 走全新安装"
}

# ---------------------------------------------------------------- 停服 + 备份
if ($isUpgrade) {
    Stop-Service $Target

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $bdir = Join-Path $Target "backups"
    if (-not (Test-Path $bdir)) { New-Item -ItemType Directory -Path $bdir | Out-Null }
    $zip = Join-Path $bdir ("upgrade-v{0}-{1}.zip" -f $oldVer, $stamp)
    Log "备份旧程序 → $zip"
    try {
        $tmp = Join-Path $env:TEMP ("lingxi_bak_" + $stamp)
        New-Item -ItemType Directory -Path $tmp | Out-Null
        Copy-Item -Path (Join-Path $Target "app") -Destination (Join-Path $tmp "app") -Recurse -Force
        if (Test-Path (Join-Path $Target "config.ini")) {
            Copy-Item (Join-Path $Target "config.ini") $tmp -Force
        }
        if (Test-Path (Join-Path $Target "VERSION")) {
            Copy-Item (Join-Path $Target "VERSION") $tmp -Force
        }
        Compress-Archive -Path (Join-Path $tmp "*") -DestinationPath $zip -CompressionLevel Optimal -Force
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Log ("备份完成：{0:N1} MB" -f ((Get-Item $zip).Length / 1MB))
    } catch {
        Log "⚠ 备份失败（继续升级）：$($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- 铺文件
if (-not (Test-Path $Target)) { New-Item -ItemType Directory -Path $Target -Force | Out-Null }

function Copy-Tree([string]$src, [string]$dst) {
    if (-not (Test-Path $src)) { return }
    $null = & robocopy $src $dst /E /NFL /NDL /NJH /NJS /R:1 /W:1
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败（$LASTEXITCODE）：$src → $dst" }
}

Log "复制程序文件（app / run / docs / 根文件）"
Copy-Tree (Join-Path $Pkg "app")  (Join-Path $Target "app")
Copy-Tree (Join-Path $Pkg "run")  (Join-Path $Target "run")
Copy-Tree (Join-Path $Pkg "docs") (Join-Path $Target "docs")
foreach ($f in @("安装升级.bat", "requirements.txt", "README.md", "VERSION", "config.example.ini")) {
    $s = Join-Path $Pkg $f
    if (Test-Path $s) { Copy-Item $s (Join-Path $Target $f) -Force }
}

# 身份标记：有它 = 建库机（能索引），没有 = 服务器
$marker = Join-Path $Target "run\deploy\sync_db.py"
if (-not (Test-Path $marker)) {
    New-Item -ItemType Directory -Path (Split-Path $marker) -Force | Out-Null
    Set-Content -Path $marker -Encoding UTF8 -Value @'
"""4.0 建库机身份标记（run/deploy/sync_db.py 存在 = 本机能索引）。"""
ROLE = "builder"
'@
    Log "补了建库机身份标记 run\deploy\sync_db.py"
}

if (-not (Test-Path (Join-Path $Target "config.ini"))) {
    # 包里带的是脱敏后的 config.ini（端口/路径都是默认值），只在没有配置时铺一份
    $src = Join-Path $Pkg "config.ini"
    if (-not (Test-Path $src)) { $src = Join-Path $Pkg "config.example.ini" }
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $Target "config.ini") -Force
        Log "没有 config.ini，已按模板生成"
    } else {
        Log "⚠ 包里没有 config.ini 模板，需要手动建一个"
    }
}
Set-Content -Path (Join-Path $Target "VERSION") -Value $newVer -Encoding UTF8

# ---------------------------------------------------------------- 依赖（只有变过才装）
if ($isUpgrade) {
    $py = Join-Path $Target "venv\Scripts\python.exe"
    $req = Join-Path $Pkg "requirements.txt"
    $hashFile = Join-Path $Target "venv\requirements.sha1"
    if ((Test-Path $py) -and (Test-Path $req)) {
        $h = (Get-FileHash $req -Algorithm SHA1).Hash
        $old = ""
        if (Test-Path $hashFile) { $old = (Get-Content $hashFile -Raw).Trim() }
        if ($h -ne $old) {
            Log "依赖清单变了 → pip install（失败不影响使用）"
            try {
                & $py -m pip install -r $req --disable-pip-version-check -q
                if ($LASTEXITCODE -eq 0) { Set-Content $hashFile $h -Encoding ASCII }
                else { Log "⚠ pip 返回 $LASTEXITCODE" }
            } catch { Log "⚠ pip 失败：$($_.Exception.Message)" }
        } else {
            Log "依赖没变，跳过 pip"
        }
    }
}

# ---------------------------------------------------------------- 启动
$startBat = Join-Path $Target "start.bat"
$hasVenv = Test-Path (Join-Path $Target "venv\Scripts\python.exe")
if ($isUpgrade -and -not $NoStart -and (Test-Path $startBat) -and $hasVenv) {
    Log "启动新版本（首次要等模型加载）"
    try { Start-Process -FilePath $startBat -WorkingDirectory $Target } catch { Log "⚠ 启动失败：$($_.Exception.Message)" }
} elseif (-not $isUpgrade) {
    Log "全新安装：下一步双击 setup.bat 装运行环境，再双击 start.bat"
} else {
    Log "已跳过自动启动（-NoStart 或没有 venv\Scripts\python.exe）"
}

Log ("完成：{0}  {1} → v{2}" -f $Target, $(if ($oldVer) { "v$oldVer" } else { "（全新）" }), $newVer)
exit 0
