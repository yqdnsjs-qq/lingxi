# -*- coding: utf-8 -*-
"""清掉某个已删除目录在索引库里的残留。

只动数据库，**不碰磁盘上的文件**。
会一并处理：chunks（片段）、faces（人脸）、files（文件）、folders（文件夹记录），
并修正 people 表的脸数和封面（避免指向已删除的人脸）。
"""
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
import sqlite3
import sys
import time
from pathlib import Path

SRC = Path(r"D:\lingxi\data\index\index.db")
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
PREFIX = _args[0] if _args else r"D:\notes"
APPLY = "--apply" in sys.argv

c = sqlite3.connect(SRC)
c.row_factory = sqlite3.Row
like = PREFIX + "%"

files = c.execute("SELECT COUNT(*) FROM files WHERE path LIKE ?", (like,)).fetchone()[0]
chunks = c.execute("SELECT COUNT(*) FROM chunks WHERE file_id IN "
                   "(SELECT id FROM files WHERE path LIKE ?)", (like,)).fetchone()[0]
faces = c.execute("SELECT COUNT(*) FROM faces WHERE file_id IN "
                  "(SELECT id FROM files WHERE path LIKE ?)", (like,)).fetchone()[0]
folders = c.execute("SELECT id, path FROM folders WHERE path LIKE ?", (like,)).fetchall()

print(f"  前缀 : {PREFIX}")
print(f"  文件 {files} · 片段 {chunks} · 人脸 {faces} · 文件夹 {len(folders)}")
for f in folders:
    print(f"    [{f['id']}] {f['path']}")
print()

if not APPLY:
    print("  （演练模式。确认后加 --apply 执行）")
    print("  只删数据库记录，磁盘上的文件和目录不动。")
    c.close()
    raise SystemExit(0)

stamp = time.strftime("%Y%m%d-%H%M%S")
bak = SRC.with_name(f"index-backup-{stamp}.db")
print(f"  备份 → {bak.name} …")
src = sqlite3.connect(SRC)
dst = sqlite3.connect(bak)
src.backup(dst)
dst.close()
src.close()
print(f"  备份完成（{bak.stat().st_size / 1e6:.0f} MB）")

with c:
    # 记下受影响的 person，稍后修正脸数 / 封面
    touched = [r[0] for r in c.execute(
        "SELECT DISTINCT person_id FROM faces WHERE person_id IS NOT NULL "
        "AND file_id IN (SELECT id FROM files WHERE path LIKE ?)", (like,))]

    c.execute("DELETE FROM chunks WHERE file_id IN "
              "(SELECT id FROM files WHERE path LIKE ?)", (like,))
    c.execute("DELETE FROM faces WHERE file_id IN "
              "(SELECT id FROM files WHERE path LIKE ?)", (like,))
    c.execute("DELETE FROM files WHERE path LIKE ?", (like,))
    c.execute("DELETE FROM folders WHERE path LIKE ?", (like,))

    # 修正人物脸数，以及被删掉的封面
    for pid in touched:
        n = c.execute("SELECT COUNT(*) FROM faces WHERE person_id=?", (pid,)).fetchone()[0]
        row = c.execute("SELECT cover_face FROM people WHERE id=?", (pid,)).fetchone()
        cover = row[0] if row else None
        if cover and not c.execute("SELECT 1 FROM faces WHERE id=?", (cover,)).fetchone():
            nxt = c.execute("SELECT id FROM faces WHERE person_id=? "
                            "ORDER BY (prob*(x2-x1)*(y2-y1)) DESC LIMIT 1", (pid,)).fetchone()
            cover = nxt[0] if nxt else None
        if n <= 0:
            c.execute("UPDATE people SET cover_face=NULL, vec=NULL, dim=NULL WHERE id=?", (pid,))
        else:
            c.execute("UPDATE people SET n_faces=?, cover_face=? WHERE id=?",
                      (n, cover, pid))

    # 全局计数兜底
    c.execute("UPDATE people SET n_faces=(SELECT COUNT(*) FROM faces "
              "WHERE person_id=people.id)")

left_f = c.execute("SELECT COUNT(*) FROM files WHERE path LIKE ?", (like,)).fetchone()[0]
print(f"  ✓ 完成。该前缀还剩 {left_f} 个文件记录")
print(f"  ✓ 修正了 {len(touched)} 个人物的脸数与封面")
tot = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
ch = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
fa = c.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
print(f"  现在总计：文件 {tot} · 片段 {ch} · 人脸 {fa}")
print(f"  想还原：把 {bak.name} 拷回 index.db（先停服务）")
c.close()
