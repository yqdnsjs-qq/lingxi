# 清理索引扫描发现的损坏文件 —— 默认移到回收站，可恢复。
#
# 用法（一般由 清理损坏文件.bat 调用）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File clean_broken.ps1 -Mode obsidian
#
# Mode:
#   list       只列清单，不删
#   obsidian   只清 D:\notes 下的（纯网页残图，零风险）
#   content    清「有内容但损坏」的（跳过 0 字节的 —— 那些可能是同步没完成）
#   all        清全部（含 0 字节）

param(
    [string]$Mode = "list",
    [switch]$Yes                       # 跳过确认（脚本内部用）
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DbPath = Join-Path $Root "data\index\index.db"
$PyExe = Join-Path $Root "venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

if (-not (Test-Path $PyExe)) { Write-Host "[错误] 找不到 $PyExe" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $DbPath)) { Write-Host "[错误] 找不到索引库 $DbPath" -ForegroundColor Red; exit 1 }

# ---------- 从索引库取损坏文件 ----------
$py = @'
import sqlite3, json, sys
patterns = ["无法识别", "0 字节", "读取图片失败", "broken data", "媒体编码失败"]
where = " OR ".join("note LIKE '%" + p + "%'" for p in patterns)
c = sqlite3.connect(sys.argv[1])
c.row_factory = sqlite3.Row
rows = c.execute(
    f"SELECT f.path, f.size, f.note, fo.path AS folder FROM files f "
    f"JOIN folders fo ON fo.id=f.folder_id WHERE {where}").fetchall()
out = []
for r in rows:
    note = r["note"] or ""
    if "0 字节" in note:
        kind = "zero"
    elif "无法识别" in note or "broken data" in note or "读取图片失败" in note:
        kind = "content"
    else:
        kind = "content"
    out.append({"path": r["path"], "size": r["size"], "kind": kind,
                "folder": r["folder"]})
print(json.dumps(out, ensure_ascii=False))
'@

$pyFile = Join-Path $env:TEMP "eg2_broken_query.py"
[System.IO.File]::WriteAllText($pyFile, $py, [System.Text.UTF8Encoding]::new($false))
$json = & $PyExe $pyFile $DbPath 2>$null
Remove-Item $pyFile -Force -ErrorAction SilentlyContinue
if (-not $json) { Write-Host "[错误] 读不出损坏文件清单" -ForegroundColor Red; exit 1 }
$items = $json | ConvertFrom-Json

# ---------- 按 Mode 过滤 ----------
$sel = switch ($Mode) {
    "obsidian" { $items | Where-Object { $_.folder -like "*obsidian*" } }
    "content"  { $items | Where-Object { $_.kind -eq "content" } }
    "all"      { $items }
    default    { @() }
}

Write-Host ""
Write-Host "  损坏文件总览：" -ForegroundColor Cyan
$items | Group-Object folder | ForEach-Object {
    $z = ($_.Group | Where-Object { $_.kind -eq "zero" }).Count
    $c = ($_.Group | Where-Object { $_.kind -eq "content" }).Count
    Write-Host ("    {0,4} 个   {1}" -f $_.Count, $_.Name)
    Write-Host ("              其中 0 字节 {0} 个 / 有内容损坏 {1} 个" -f $z, $c) -ForegroundColor DarkGray
}
Write-Host ""

if ($Mode -eq "list" -or $sel.Count -eq 0) {
    if ($Mode -ne "list") { Write-Host "  本次没有要处理的文件。" -ForegroundColor Yellow }
    Write-Host "  完整清单：" -ForegroundColor DarkGray
    Write-Host "    $Root\损坏文件清单.txt" -ForegroundColor DarkGray
    Write-Host "    $Root\损坏文件清单.csv" -ForegroundColor DarkGray
    exit 0
}

$totalSize = ($sel | Measure-Object -Property size -Sum).Sum
Write-Host "  本次将处理：" -ForegroundColor Yellow
Write-Host ("    数量 {0} 个，合计 {1:N1} MB" -f $sel.Count, ($totalSize / 1MB))
Write-Host "    方式：移到「回收站」（可恢复，不是永久删除）" -ForegroundColor Green
Write-Host ""

if (-not $Yes) {
    $ans = Read-Host "  确认执行？输入 YES 继续，其它任意键取消"
    if ($ans -ne "YES") { Write-Host "  已取消。" -ForegroundColor DarkGray; exit 0 }
}

# ---------- 移到回收站 ----------
Add-Type -AssemblyName Microsoft.VisualBasic
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logFile = Join-Path $LogDir "cleanup-$stamp.log"
$okCount = 0; $fail = @(); $skip = @()

foreach ($it in $sel) {
    $p = $it.path
    if (-not (Test-Path -LiteralPath $p)) { $skip += $p; continue }
    try {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            $p,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
        Add-Content -LiteralPath $logFile -Value ("删除`t{0}`t{1}" -f $it.size, $p) -Encoding UTF8
        $okCount++
        if ($okCount % 20 -eq 0) { Write-Host ("    已处理 {0}/{1}…" -f $okCount, $sel.Count) -ForegroundColor DarkGray }
    } catch {
        Add-Content -LiteralPath $logFile -Value ("失败`t{0}`t{1}`t{2}" -f $it.size, $p, $_.Exception.Message) -Encoding UTF8
        $fail += $p
    }
}

Write-Host ""
Write-Host ("  完成：成功 {0} 个，失败 {1} 个，跳过（已不存在）{2} 个" -f $okCount, $fail.Count, $skip.Count) -ForegroundColor Green
Write-Host ("  日志：{0}" -f $logFile) -ForegroundColor DarkGray
if ($fail.Count) {
    Write-Host "  失败的文件（前 5 个）：" -ForegroundColor Yellow
    $fail | Select-Object -First 5 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkYellow }
}
Write-Host ""
Write-Host "  提示：这些文件已从回收站可恢复。" -ForegroundColor DarkGray
Write-Host "        清理后回到程序点一次「增量更新」，索引库就干净了，日志也不再刷警告。" -ForegroundColor DarkGray
