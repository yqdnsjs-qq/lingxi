r"""把索引库里记录的路径前缀换成新的访问路径。

为什么可以这么做
----------------
库里所有数据（向量、人脸、片段）都是挂在 **文件 id** 上的，而 id 从不变化。
路径只是"怎么找到这个文件"的字符串。所以只要 NAS 上的**内容没动**
（还是那些文件、大小和修改时间不变），换个访问入口（\\NAS\photo → P:\）
只需要把路径字符串的前缀替换掉，其它全都不用重扫。

典型用法
--------
    # 先演练，看看会改什么（不写库）
    venv\Scripts\python.exe app\remap_paths.py --old "\\NAS\photo" --new "P:"

    # 确认没问题再执行
    venv\Scripts\python.exe app\remap_paths.py --old "\\NAS\photo" --new "P:" --apply

    # 搬到 Linux 服务器（目标是 / 开头时自动按 POSIX 处理：剩余路径的 \ 会转成 /）
    ./venv/bin/python app/remap_paths.py --old '\\NAS\photo' --new /mnt/nas_photo --apply

执行前会自动备份整个索引库。
"""
from __future__ import annotations

# ---- Windows GBK 控制台保护 ----
# 这个脚本会打印 ✓ ✗ ⚠ 这类字符，cp936 编不出来会直接 UnicodeEncodeError 崩掉。
# 改成"编不出就降级成 ?"，中文照常显示。
import sys as _sys

try:
    _sys.stdout.reconfigure(errors="replace")
    _sys.stderr.reconfigure(errors="replace")
except Exception:                                          # noqa: BLE001
    pass
# ---- 保护结束 ----

import argparse
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "index" / "index.db"


def norm(p: str, posix: bool = False) -> str:
    """归一化前缀。

    posix=True 用于**目标在 Linux 上**的场景（比如把库搬到 Ubuntu 服务器）：
    这时不能像 Windows 那样把 `/` 全转成 `\\`，否则 /home/lingxi/... 会变成
    \\home\\codex\\... —— 而且剩余部分的反斜杠在 Linux 上会被当成文件名的一部分，
    文件永远读不到。
    """
    p = (p or "").strip()
    if posix:
        return p.replace("\\", "/").rstrip("/")
    return p.replace("/", "\\").rstrip("\\")


def norm_rest(rest: str, posix: bool) -> str:
    """前缀之后的那一段：POSIX 目标要把反斜杠换成斜杠。"""
    return rest.replace("\\", "/") if posix else rest


def apply_remap(conn: sqlite3.Connection, old: str, new: str, style: str = "keep") -> int:
    """把库里 old 前缀改成 new，返回改动的文件行数。

    style 决定「前缀之后那一段」的斜杠风格：
      · "keep"    原样保留（Windows → Windows，比如 \\\\NAS\\photo → P:）
      · "posix"   反斜杠转斜杠（Windows → Linux，比如 \\\\NAS\\photo → /mnt/photo）
      · "windows" 斜杠转反斜杠（Linux → Windows，把服务器上的库拉回本机时用）

    抽成函数是为了让「同步库到服务器 / 从服务器拉回来」也能用（sync_db.py 会先在
    快照副本上翻译路径，避免把对端的路径风格覆盖掉）。
    """
    like_bs = old + "\\%"
    like_fs = old + "/%"
    tail = f"substr(path, {len(old) + 1})"
    if style == "posix":
        tail = f"replace(substr(path, {len(old) + 1}), '\\', '/')"
    elif style == "windows":
        tail = f"replace(substr(path, {len(old) + 1}), '/', '\\')"
    n = conn.execute(
        "SELECT COUNT(*) FROM files WHERE path = ? OR path LIKE ? OR path LIKE ?",
        (old, like_bs, like_fs)).fetchone()[0]
    with conn:
        conn.execute(
            f"UPDATE folders SET path = ? || {tail} WHERE path = ? OR path LIKE ? OR path LIKE ?",
            (new, old, like_bs, like_fs))
        conn.execute(
            f"UPDATE files SET path = ? || {tail} WHERE path = ? OR path LIKE ? OR path LIKE ?",
            (new, old, like_bs, like_fs))
        conn.execute(
            f"UPDATE files SET dir = ? || {tail} WHERE dir = ? OR dir LIKE ? OR dir LIKE ?",
            (new, old, like_bs, like_fs))
    return int(n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="旧前缀，如 \\\\NAS\\photo 或 D:\\tmp\\qx")
    ap.add_argument("--new", required=True, help="新前缀，如 P: 或 /mnt/photo")
    ap.add_argument("--apply", action="store_true", help="真的写库（不加只演练）")
    ap.add_argument("--linux", action="store_true",
                    help="目标在 Linux 上（--new 以 / 开头时自动开启）：剩余路径转成 /")
    ap.add_argument("--windows", action="store_true",
                    help="目标是 Windows（--new 形如 D: 或 UNC 共享）：剩余路径转成反斜杠")
    args = ap.parse_args()

    to_posix = args.linux or args.new.strip().startswith("/")
    looks_windows = bool(re.match(r"^[A-Za-z]:", args.new.strip())) or args.new.strip().startswith("\\\\")
    style = "posix" if to_posix else ("windows" if (args.windows or looks_windows) else "keep")
    old = norm(args.old, posix=False)          # 库里存的是 Windows 风格
    new = norm(args.new, posix=to_posix)
    if not old or old == new:
        print("  旧前缀与新前缀不能相同")
        return 1
    if not DB.exists():
        print(f"  找不到索引库：{DB}")
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    like_bs = old + "\\%"
    like_fs = old + "/%"

    folders = conn.execute(
        "SELECT id, path FROM folders WHERE path = ? OR path LIKE ? OR path LIKE ?",
        (old, like_bs, like_fs)).fetchall()
    n_files = conn.execute(
        "SELECT COUNT(*) c FROM files WHERE path LIKE ? OR path = ? OR path LIKE ?",
        (like_bs, old, like_fs)).fetchone()["c"]
    n_dirs = conn.execute(
        "SELECT COUNT(DISTINCT dir) c FROM files WHERE dir LIKE ? OR dir = ? OR dir LIKE ?",
        (like_bs, old, like_fs)).fetchone()["c"]

    print(f"  旧前缀 : {old}")
    print(f"  新前缀 : {new}   （斜杠风格：{style}）")
    print()
    print(f"  会改动：")
    print(f"    文件夹 {len(folders)} 个")
    print(f"    文件   {n_files} 个")
    print(f"    目录   {n_dirs} 个")
    print()
    if folders:
        print("  文件夹路径变化：")
        for f in folders:
            print(f"    [{f['id']:>2}] {f['path']}")
            print(f"         → {new + norm_rest(f['path'][len(old):], to_posix)}")
        print()

    # 抽样验证新路径是否真的存在（只验第一个，避免读 NAS 太慢）
    sample = conn.execute(
        "SELECT path FROM files WHERE path LIKE ? OR path LIKE ? LIMIT 1",
        (like_bs, like_fs)).fetchone()
    if sample:
        np_ = new + norm_rest(sample["path"][len(old):], style)
        exists = os.path.isfile(np_)
        print(f"  抽样验证：")
        print(f"    旧 {sample['path']}")
        print(f"    新 {np_}")
        print(f"    新路径存在：{'✓ 是' if exists else '✗ 否 —— 先别执行！'}")
        if not exists:
            print()
            print("  ⚠ 新路径读不到。请确认：")
            print("     · 盘符 / 共享已挂载")
            print("     · 结构还是「<新前缀>/<原来的子路径>」")
            print("     确认无误后再加 --apply")
        print()

    if not args.apply:
        print("  （演练模式，没有改任何数据。确认无误后加 --apply 执行）")
        conn.close()
        return 0

    # 备份
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = DB.with_name(f"index-backup-{stamp}.db")
    print(f"  正在备份索引库 → {bak.name} …")
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(bak)
    src.backup(dst)
    dst.close()
    src.close()
    print(f"  备份完成（{bak.stat().st_size / 1e6:.0f} MB）")
    print()

    with conn:
        # 前缀之后的部分：POSIX 目标要把 '\' 换成 '/'（SQLite 的 replace 是 3 参数）
        apply_remap(conn, old, new, style)

    print("  ✓ 已更新。现在的文件夹路径：")
    for r in conn.execute("SELECT id, path FROM folders ORDER BY id"):
        print(f"    [{r['id']:>2}] {r['path']}")
    print()
    print("  下一步：回程序点一次「增量更新」。")
    print("  文件大小和修改时间没变 → 会全部跳过，不会重扫、不会重算向量。")
    print(f"  想还原：把 {bak.name} 拷回 index.db（先停服务）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
