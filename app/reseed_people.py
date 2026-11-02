"""给「一张脸都没分到」的人物重新播种。

问题
----
自动归属用的门槛比较严（照片外 0.74），于是这些人一张脸都没分到：
    周八 0/59、艾怡萱 0/45、朱琳 2/26、刘二老公 2/31 …

但他们的**标注照片是可信的证据** —— 那些照片里确实有他。
所以对他们单独放宽：只在**他自己标注过的照片**范围内，按相似度收脸。
照片是"有他在"的硬证据，门槛可以低一些（0.52），
比在全库里放宽安全得多。

顺便把他们已有的脸也算进质心，迭代一轮。
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

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(r"D:\lingxi")
DB = ROOT / "data" / "index" / "index.db"
PEOPLE_JSON = ROOT / "face-lab" / "out" / "people.json"

IN_PHOTO = 0.52          # 在本人标注照片里的脸
ROUNDS = 2


def vec_of(blob):
    if not blob:
        return None
    v = np.frombuffer(blob, dtype=np.float32)
    if v.size == 0 or not np.isfinite(v).all():
        return None
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else None


def main() -> int:
    data = json.loads(PEOPLE_JSON.read_text(encoding="utf-8"))
    obj = {(o.get("name") or "").strip() for o in (data.get("objects") or [])}
    owner_files: dict[str, set[str]] = defaultdict(set)
    for p in data.get("people") or []:
        nm = (p.get("name") or "").strip()
        if not nm or nm in obj:
            continue
        for ph in p.get("photos") or []:
            owner_files[nm].add(Path(ph).name.lower())

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT f.id, f.vec, f.person_id, fi.name AS fname "
        "FROM faces f JOIN files fi ON fi.id=f.file_id").fetchall()
    V, FID, FNAME, PID = [], [], [], []
    for r in rows:
        v = vec_of(r["vec"])
        if v is None:
            continue
        V.append(v); FID.append(int(r["id"]))
        FNAME.append((r["fname"] or "").lower())
        PID.append(r["person_id"])
    V = np.stack(V) if V else np.zeros((0, 512), dtype=np.float32)
    PID = np.array(PID, dtype=object)

    named = conn.execute(
        "SELECT id, name FROM people WHERE name IS NOT NULL AND name<>''").fetchall()
    # 只挑「分到的脸少于标注照片数的 30%」的人
    targets = []
    for p in named:
        cnt = int((PID == p["id"]).sum()) if len(PID) else 0
        want = len(owner_files.get(p["name"], ()))
        if want and cnt < want * 0.3:
            targets.append((p["id"], p["name"], cnt, want))
    print(f"  需要重新播种的人物 {len(targets)} 个：")
    for _pid, nm, cnt, want in targets:
        print(f"    {nm[:18]:20s} 现有 {cnt:>4} 张 / 标注照片 {want:>4} 张")
    print()

    total_added = 0
    for pid, nm, cnt, want in targets:
        own = owner_files.get(nm, set())
        # 有据可依的候选：脸出现在他标注过的照片里
        cand = np.array([i for i, fn in enumerate(FNAME) if fn in own], dtype=int)
        if not len(cand):
            continue
        c = None
        if cnt:
            sel = np.where(PID == pid)[0]
            if len(sel):
                c = V[sel].mean(axis=0)
        if c is None and len(cand):
            # 没有现成的脸，就用候选里"最密集"的那一群起步
            c = V[cand].mean(axis=0)
        n = float(np.linalg.norm(c))
        if n < 1e-6:
            continue
        c = c / n

        added = 0
        for _ in range(ROUNDS):
            sims = V[cand] @ c
            take = cand[sims >= IN_PHOTO]
            if not len(take):
                break
            cc = V[take].mean(axis=0)
            nn = float(np.linalg.norm(cc))
            if nn < 1e-6:
                break
            c = cc / nn
        sims = V[cand] @ c
        take = cand[sims >= IN_PHOTO]
        for i in take:
            if PID[i] is None:
                conn.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, FID[i]))
                added += 1
        conn.execute("UPDATE people SET n_faces=(SELECT COUNT(*) FROM faces "
                     "WHERE person_id=?) WHERE id=?", (pid, pid))
        # 更新质心
        sel = np.where(PID == pid)[0]
        allsel = np.array([i for i in take] + list(sel), dtype=int)
        if len(allsel):
            cc = V[allsel].mean(axis=0)
            nn = float(np.linalg.norm(cc))
            if nn > 1e-6:
                conn.execute("UPDATE people SET vec=?, dim=? WHERE id=?",
                             ((cc / nn).astype(np.float32).tobytes(),
                              int((cc / nn).shape[0]), pid))
        total_added += added
        print(f"    {nm[:18]:20s} 新归入 {added:>4} 张")

    conn.commit()
    print()
    print(f"  ✓ 共补回 {total_added} 张脸")
    print()
    print("  现在的分布：")
    for r in conn.execute(
            "SELECT p.name, COUNT(f.id) n FROM people p "
            "LEFT JOIN faces f ON f.person_id=p.id "
            "WHERE p.name IS NOT NULL AND p.name<>'' "
            "GROUP BY p.id ORDER BY n DESC LIMIT 12"):
        print(f"    {r[0][:20]:22s} {r[1]:>5} 张")
    left = conn.execute("SELECT COUNT(*) FROM faces WHERE person_id IS NULL").fetchone()[0]
    print(f"\n  仍未归属 {left} 张")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
