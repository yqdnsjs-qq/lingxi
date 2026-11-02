"""把 face-lab 阶段标注的人物名字导入网页库。

核心思路（不依赖"单脸照片"）
--------------------------
people.json 里每个人带着**照片路径列表**。文件名两边 100% 对得上，
所以「某张照片属于某人」。一张合影里可能有多张脸，不能直接全归给同一个人。

于是用**迭代精修**：
    ① 初值：对每个人，把他照片里出现的脸都先当成"可能是他"，
       每张照片只取一张脸（避免脸多的照片压过整体），算出一个初始质心
    ② 迭代 3 轮：
       把所有脸按"离谁质心最近"重新分配
       → 用新分配的脸重算质心
       → 因为初值已经偏向正确方向，几轮就收敛
    ③ 只保留相似度够高的归属，剩下的交给界面上的「重新聚类」

同时把质心存进 people.vec —— 以后重新扫描/聚类能按质心自动认领名字。
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
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(r"D:\lingxi")
DB = ROOT / "data" / "index" / "index.db"
PEOPLE_JSON = ROOT / "face-lab" / "out" / "people.json"

# 最终归属门槛。之前是 0.50，实测偏松：
#   同一个人 中位数 0.88，不同的人 0.32 —— 0.50 落在模糊区，
#   长得像的家人（姐妹、表妹）经常被判错。
# 提到 0.62 明显更准；代价是归属的脸少一些，剩下的会进「重新聚类」
# 作为待命名的新组，可以人工补。
ASSIGN = 0.62            # 在本人标注照片里的脸，用这个门槛
MARGIN = 0.06            # 还要比"第二名"高出这么多，避免模棱两可
# 不在本人标注照片里的脸：要求高得多。
# 为什么：样本少的人（表妹只有 7 张标注照片）建出的质心不可靠，
# 迭代时会把大量别人的脸吸过来（实测 7 张标注 → 抢到 332 张脸）。
# 照片外的脸必须"非常像"才收，靠照片内的脸提供可信的召回。
ASSIGN_OUT = 0.74
ROUNDS = 3


def vec_of(blob):
    """人脸向量。注意：库里存的是 **float32**（512 维 = 2048 字节），
    不是 float16 —— 按 float16 读会变成一堆 NaN，整个匹配就静默失效。"""
    if not blob:
        return None
    raw = np.frombuffer(blob, dtype=np.float32)
    if raw.size == 0 or not np.isfinite(raw).all():
        return None
    n = float(np.linalg.norm(raw))
    return raw / n if n > 1e-6 else None


def _accept(sims, best, bestv, in_own=None):
    """够像 **而且** 明显领先第二名，才认这个归属。

    两个门槛：
      · 这张脸在"本人标注过的照片"里 → ASSIGN(0.62)，有据可依
      · 在别人的照片里             → ASSIGN_OUT(0.74)，必须非常像
    """
    thr = np.full(len(bestv), ASSIGN_OUT, dtype=np.float32)
    if in_own is not None and len(in_own):
        thr = np.where(in_own, ASSIGN, ASSIGN_OUT)
    ok = bestv >= thr
    if sims.shape[1] > 1:
        tmp = sims.copy()
        tmp[np.arange(len(best)), best] = -9.0
        second = tmp.max(axis=1)
        ok &= (bestv - second) >= MARGIN
    return ok


def centroid(mat):
    if not len(mat):
        return None
    c = np.mean(mat, axis=0)
    n = float(np.linalg.norm(c))
    return c / n if n > 1e-6 else None


def main() -> int:
    data = json.loads(PEOPLE_JSON.read_text(encoding="utf-8"))
    obj = {(o.get("name") or "").strip() for o in (data.get("objects") or [])}
    file_owner: dict[str, str] = {}
    for p in data.get("people") or []:
        nm = (p.get("name") or "").strip()
        if not nm or nm in obj:
            continue
        for ph in p.get("photos") or []:
            file_owner[Path(ph).name.lower()] = nm
    names = sorted(set(file_owner.values()))
    print(f"  people.json：{len(names)} 人 / {len(file_owner)} 张标注照片")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(people)")}
    if "vec" not in cols:
        conn.execute("ALTER TABLE people ADD COLUMN vec BLOB")
        conn.execute("ALTER TABLE people ADD COLUMN dim INTEGER")
        conn.commit()

    rows = conn.execute(
        "SELECT f.id, f.vec, fi.name AS fname FROM faces f "
        "JOIN files fi ON fi.id=f.file_id").fetchall()
    print(f"  库里人脸 {len(rows)} 张")

    V, FID, FNAME = [], [], []
    for r in rows:
        v = vec_of(r["vec"])
        if v is None:
            continue
        V.append(v)
        FID.append(r["id"])
        FNAME.append((r["fname"] or "").lower())
    V = np.stack(V)
    print(f"  有效向量 {len(V)} 张")

    idx_by_name: dict[str, list[int]] = defaultdict(list)
    for i, fn in enumerate(FNAME):
        nm = file_owner.get(fn)
        if nm:
            idx_by_name[nm].append(i)

    # ---------- ① 初值：每人每张照片取一张脸 ----------
    cents: dict[str, np.ndarray] = {}
    for nm in names:
        idxs = idx_by_name.get(nm) or []
        seen, picked = set(), []
        for i in idxs:
            if FNAME[i] in seen:
                continue
            seen.add(FNAME[i])
            picked.append(i)
        c = centroid(V[picked]) if picked else None
        if c is not None:
            cents[nm] = c
    print(f"  初始质心 {len(cents)} 个")

    # ---------- ② 迭代精修 ----------
    order = list(cents)
    mat = np.stack([cents[n] for n in order])
    for rd in range(ROUNDS):
        sims = V @ mat.T                     # (N, P)
        best = sims.argmax(axis=1)
        bestv = sims[np.arange(len(V)), best]
        ok = _accept(sims, best, bestv,
                         np.array([file_owner.get(FNAME[i]) == order[j]
                                   for i, j in enumerate(best)]))
        new = {}
        for j, nm in enumerate(order):
            sel = ok & (best == j)
            c = centroid(V[sel]) if sel.sum() >= 1 else None
            if c is None:
                # 这一轮一张都没分到 —— **不要删掉这个人**，
                # 保留上一轮的质心。之前这里直接丢弃，导致样本少的人
                # （吴九原本 434 张脸）整名字消失。
                c = mat[j]
            new[nm] = c
        order = list(new)
        mat = np.stack([new[n] for n in order])
        print(f"    第 {rd+1} 轮：归属 {int(ok.sum())} 张，"
              f"质心 {len(order)} 个，平均相似度 "
              f"{float(bestv[ok].mean()) if ok.any() else 0:.3f}")

    # ---------- ③ 写库 ----------
    sims = V @ mat.T
    best = sims.argmax(axis=1)
    bestv = sims[np.arange(len(V)), best]
    ok = _accept(sims, best, bestv,
                         np.array([file_owner.get(FNAME[i]) == order[j]
                                   for i, j in enumerate(best)]))

    conn.execute("UPDATE faces SET person_id=NULL")
    conn.execute("DELETE FROM people")
    conn.commit()

    pids = {}
    for j, nm in enumerate(order):
        cur = conn.execute(
            "INSERT INTO people (name, kind, n_faces, created) "
            "VALUES (?,?,0,strftime('%s','now'))", (nm, "person"))
        pids[nm] = int(cur.lastrowid)
    n_write = 0
    for i in np.where(ok)[0]:
        conn.execute("UPDATE faces SET person_id=? WHERE id=?",
                     (pids[order[int(best[i])]], int(FID[int(i)])))
        n_write += 1
    conn.commit()

    for nm, pid in pids.items():
        sel = ok & np.array([order[int(b)] == nm for b in best])
        if sel.sum():
            c = centroid(V[sel])
            if c is not None:
                conn.execute(
                    "UPDATE people SET vec=?, dim=?, n_faces=? WHERE id=?",
                    (c.astype(np.float32).tobytes(), int(c.shape[0]), int(sel.sum()), pid))
    # 给每个人物设封面脸 —— 没有它，界面上的头像卡片是空白的。
    # 挑「置信度高 x 脸大」的那张，通常最清晰、最正面。
    conn.execute(
        "UPDATE people SET cover_face = ("
        "  SELECT f.id FROM faces f WHERE f.person_id = people.id"
        "  ORDER BY (f.prob * (f.x2-f.x1) * (f.y2-f.y1)) DESC LIMIT 1"
        ") WHERE EXISTS (SELECT 1 FROM faces f WHERE f.person_id = people.id)")
    conn.commit()

    print()
    print(f"  ✓ 命名 {len(pids)} 个人物，归属 {n_write} 张脸")
    print()
    print("  人物排行（前 20）：")
    for r in conn.execute(
            "SELECT p.name, COUNT(f.id) n FROM people p "
            "LEFT JOIN faces f ON f.person_id=p.id "
            "WHERE p.name IS NOT NULL AND p.name<>'' "
            "GROUP BY p.id ORDER BY n DESC LIMIT 20"):
        print(f"    {r[0][:22]:24s} {r[1]:>5} 张脸")
    left = conn.execute("SELECT COUNT(*) FROM faces WHERE person_id IS NULL").fetchone()[0]
    print(f"\n  还有 {left} 张脸未归属 → 界面点「重新聚类」成为待命名的新组")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
