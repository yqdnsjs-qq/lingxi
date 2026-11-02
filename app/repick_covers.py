"""重新挑选每个人物的封面脸。

原来的规则是「脸最大 x 置信度最高」，问题是：
    合影里最大的一张脸可能是侧脸、背影、或者糊掉的，
    虽然确实属于这个人，但拿来做头像很丑。

改成：**取离该人物向量质心最近的那张脸**（即"最像他本人"），
再要求脸足够大（太小的做头像也看不清）。

质心已经在导入人名时存进 people.vec 了，直接用。
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

import sqlite3
from pathlib import Path

import numpy as np

DB = Path(r"D:\lingxi\data\index\index.db")
MIN_W = 90          # 脸宽下限（像素），太小做头像看不清


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    people = conn.execute(
        "SELECT id, name, vec FROM people "
        "WHERE name IS NOT NULL AND name<>''").fetchall()
    print(f"  已命名人物 {len(people)} 个")

    updated = skipped = 0
    for p in people:
        blob = p["vec"]
        if not blob:
            # 没有质心就退回"最大的脸"
            row = conn.execute(
                "SELECT id FROM faces WHERE person_id=? "
                "ORDER BY (prob*(x2-x1)*(y2-y1)) DESC LIMIT 1", (p["id"],)).fetchone()
            if row:
                conn.execute("UPDATE people SET cover_face=? WHERE id=?",
                             (row["id"], p["id"]))
                updated += 1
            else:
                skipped += 1
            continue

        c = np.frombuffer(blob, dtype=np.float32)
        n = float(np.linalg.norm(c))
        if n > 1e-6:
            c = c / n
        rows = conn.execute(
            "SELECT id, vec, (x2-x1) w FROM faces WHERE person_id=?", (p["id"],)).fetchall()
        best, best_score = None, -9.0
        for r in rows:
            if not r["vec"] or (r["w"] or 0) < MIN_W:
                continue
            v = np.frombuffer(r["vec"], dtype=np.float32)
            vn = float(np.linalg.norm(v))
            if vn < 1e-6:
                continue
            sim = float(c @ (v / vn))
            # 相似度为主，脸大小作为轻微加权（避免挑到刚好够线的小脸）
            score = sim + 0.02 * min(float(r["w"]) / 1000.0, 1.0)
            if score > best_score:
                best, best_score = int(r["id"]), score
        if best is None:                     # 都不够大，放宽再选一次
            for r in rows:
                if not r["vec"]:
                    continue
                v = np.frombuffer(r["vec"], dtype=np.float32)
                vn = float(np.linalg.norm(v))
                if vn < 1e-6:
                    continue
                sim = float(c @ (v / vn))
                if sim > best_score:
                    best, best_score = int(r["id"]), sim
        if best is not None:
            conn.execute("UPDATE people SET cover_face=? WHERE id=?",
                         (best, p["id"]))
            updated += 1
        else:
            skipped += 1

    conn.commit()
    print(f"  ✓ 重新选定封面 {updated} 个，跳过 {skipped} 个")
    print()
    print("  抽样（相似度越高说明越像本人）：")
    for r in conn.execute(
            "SELECT p.name, p.cover_face, (f.x2-f.x1) w, f.prob "
            "FROM people p JOIN faces f ON f.id=p.cover_face "
            "WHERE p.name IS NOT NULL AND p.name<>'' "
            "ORDER BY p.n_faces DESC LIMIT 10"):
        print(f"    {r['name'][:18]:20s} 封面={r['cover_face']:<6} "
              f"脸宽={r['w']:>5.0f}px 置信度={r['prob']:.3f}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
