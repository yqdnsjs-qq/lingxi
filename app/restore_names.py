# -*- coding: utf-8 -*-
"""从备份里把「人物名字 → 人脸」的对应关系捞回当前库。

背景
----
点了「重新聚类」之后，分组被整体重建，32 个人的名字丢了
（当前只剩 12 个已命名组）。人脸本身没丢，只是不知道是谁。

为什么不能整库还原
------------------
备份是几小时前的，还原会丢掉这段时间的扫描成果：
  · TEMP 新提取的 5253 张脸
  · 新相册的索引

所以只捞「名字」这一层：
  1. 从备份读 people（名字）+ faces（哪张脸属于哪个人）
  2. 用 **文件路径 + 人脸坐标** 在**当前库**里找到同一张脸
     （不能用 file_id，两个库的 id 不一样）
  3. 把当前库那张脸的 person_id 改成同名人物
  4. 当前库缺的人物记录补上

人脸坐标是 MTCNN 检出来的像素框，同一张照片结果稳定，所以能可靠匹配。

用法：
    venv\\Scripts\\python.exe app\\restore_names.py                 # 演练
    venv\\Scripts\\python.exe app\\restore_names.py --apply         # 执行
    venv\\Scripts\\python.exe app\\restore_names.py --src <备份路径> --apply
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
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CUR = ROOT / "data" / "index" / "index.db"
DEFAULT_SRC = ROOT / "data" / "index" / "people-backup2.db"


def norm(p: str) -> str:
    return (p or "").replace("/", "\\").strip().lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="先清空现有的全部归属和人物，再套用备份（消除聚类叠加）")
    args = ap.parse_args()

    src_path = Path(args.src)
    if not src_path.is_file():
        print(f"  找不到备份：{src_path}")
        return 1

    cur = sqlite3.connect(CUR)
    cur.row_factory = sqlite3.Row
    bak = sqlite3.connect(src_path)
    bak.row_factory = sqlite3.Row

    print(f"  来源备份: {src_path.name}")
    print(f"  当前库  : {CUR.name}")
    print()

    # ---- 备份里的 名字 → 该人物的所有脸（路径 + 坐标）----
    bak_people = {r["id"]: (r["name"] or "").strip()
                  for r in bak.execute("SELECT id, name FROM people")}
    rows = bak.execute(
        "SELECT fa.person_id, fa.x1, fa.y1, fa.x2, fa.y2, fi.path AS fpath "
        "FROM faces fa JOIN files fi ON fi.id = fa.file_id "
        "WHERE fa.person_id IS NOT NULL").fetchall()

    wanted: dict[str, set[tuple]] = {}
    for r in rows:
        nm = bak_people.get(r["person_id"], "")
        if not nm:
            continue
        wanted.setdefault(nm, set()).add(
            (norm(r["fpath"]), int(r["x1"] or 0), int(r["y1"] or 0),
             int(r["x2"] or 0), int(r["y2"] or 0)))
    print(f"  备份里带名字的人物 {len(wanted)} 个，"
          f"共 {sum(len(v) for v in wanted.values())} 条人脸记录")
    print()

    # ---- 当前库里所有脸，建索引 ----
    cur_faces: dict[tuple, int] = {}
    for r in cur.execute(
            "SELECT fa.id, fa.x1, fa.y1, fa.x2, fa.y2, fi.path AS fpath "
            "FROM faces fa JOIN files fi ON fi.id = fa.file_id"):
        cur_faces[(norm(r["fpath"]), int(r["x1"] or 0), int(r["y1"] or 0),
                   int(r["x2"] or 0), int(r["y2"] or 0))] = int(r["id"])
    print(f"  当前库人脸 {len(cur_faces)} 张")

    # ---- 当前库已有的人物（同名复用）----
    have = {}
    for r in cur.execute("SELECT id, name FROM people WHERE name IS NOT NULL AND name<>''"):
        have[(r["name"] or "").strip()] = int(r["id"])

    plan = {}
    missing_people = []
    for nm, keys in wanted.items():
        hit = [cur_faces[k] for k in keys if k in cur_faces]
        if not hit:
            continue
        plan[nm] = hit
        if nm not in have:
            missing_people.append(nm)

    print(f"  能匹配上的名字 {len(plan)} 个（{sum(len(v) for v in plan.values())} 张脸）")
    if missing_people:
        print(f"  当前库缺这些人物记录，会新建 {len(missing_people)} 个：")
        for nm in missing_people[:12]:
            print(f"    · {nm}")
    print()
    print("  匹配数量（前 12）：")
    for nm, hit in sorted(plan.items(), key=lambda kv: -len(kv[1]))[:12]:
        print(f"    {nm[:20]:22s} {len(hit):>5} 张")
    print()

    if not args.apply:
        print("  （演练模式，没有改动。确认后加 --apply 执行）")
        cur.close(); bak.close()
        return 0

    # ---- 备份当前库 ----
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst_path = CUR.with_name(f"index-backup-{stamp}.db")
    print(f"  备份当前库 → {dst_path.name} …")
    a = sqlite3.connect(CUR); b = sqlite3.connect(dst_path)
    a.backup(b); b.close(); a.close()
    print(f"  备份完成（{dst_path.stat().st_size / 1e6:.0f} MB）")

    with cur:
        if args.reset:
            # 关键：先把「重新聚类」产生的一切归属和人物清空。
            # 否则会变成「聚类分组 + 备份名字」叠加，数字被放大
            # （实测张三 710 张会变成 762 张，吴九 59 张变成 455 张）。
            n_old = cur.execute("SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL").fetchone()[0]
            cur.execute("UPDATE faces SET person_id=NULL")
            cur.execute("DELETE FROM people")
            print(f"  已清空 {n_old} 张脸的归属和全部人物记录")
            have = {}
        for nm, hit in plan.items():
            pid = have.get(nm)
            if pid is None:
                kind = "person"
                cur.execute(
                    "INSERT INTO people(name, kind, n_faces, created) "
                    "VALUES(?,?,?,?)", (nm, kind, 0, time.time()))
                pid = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
                have[nm] = pid
            # 把之前聚类分错的脸纠正回来
            for i in range(0, len(hit), 400):
                batch = hit[i:i + 400]
                cur.execute(
                    f"UPDATE faces SET person_id=? WHERE id IN "
                    f"({','.join('?' * len(batch))})", [pid, *batch])

        # 重建每个人物的脸数 + 质心 + 封面
        for nm, pid in have.items():
            n = cur.execute("SELECT COUNT(*) FROM faces WHERE person_id=?",
                            (pid,)).fetchone()[0]
            if n <= 0:
                cur.execute("UPDATE people SET n_faces=0, cover_face=NULL, "
                            "vec=NULL, dim=NULL WHERE id=?", (pid,))
                continue
            covers = cur.execute(
                "SELECT id FROM faces WHERE person_id=? "
                "ORDER BY (prob*(x2-x1)*(y2-y1)) DESC LIMIT 1", (pid,)).fetchone()
            cur.execute("UPDATE people SET n_faces=?, cover_face=? WHERE id=?",
                        (n, covers[0] if covers else None, pid))

    n_named = cur.execute("SELECT COUNT(*) FROM people WHERE name IS NOT NULL "
                          "AND name<>'' AND n_faces>0").fetchone()[0]
    a_own = cur.execute("SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL").fetchone()[0]
    tot = cur.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
    print()
    print(f"  ✓ 恢复完成：已命名并有脸的人物 {n_named} 个")
    print(f"    已归属 {a_own} / {tot} 张脸")
    print(f"  想还原：把 {dst_path.name} 拷回 index.db（先停服务）")
    print()
    print("  下一步：回界面点「人物」刷新看看。")
    print("  注意：**不要再点「重新聚类」** —— 那会再次打散。")
    cur.close(); bak.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
