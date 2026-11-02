"""文件夹「共享 / 私有」设置的双向合并（多人 P3-e）。

解决的问题：推库是"**本机数据库整份覆盖服务器**" ✗ —— 于是**在服务器上**把
某个文件夹设成私有，下一次同步就被本机那份（还是共享）覆盖回去 ✗，
人还以为设置生效了。这个模块让两边像账号那样合并：

  · 给 `folders` 加 `updated_at`；用**触发器**维护 ——
    只在 `visibility` / `owner_id` / `config` 真的变化时刷新 ✓
    （`last_scan` 等日常更新不算"改设置" ✓）
  · 合并规则：**同一个文件夹，谁的设置更新就用谁**（整行取更新那侧的
    visibility + owner_id ✓），`updated_at` 相同/缺失则保留本机那份 ✓（幂等 ✓）
  · **绝不新增/删除文件夹**，也**绝不改 path** ✗ ——
    只动设置。删除文件夹、新增文件夹都必须由人在界面上显式操作 ✓
    （曾经想过"服务器上新增的文件夹自动建到本机"，但两侧路径不同
      —— 本机是 Windows 路径、服务器是 Linux 路径 —— 自动建会指向不存在的
      位置，索引器会报错 ✗，所以明确不做 ✓）

身份用 `id`：两边这份库本来是同一份（服务器是从本机推过去的 ✓），所以 id 对应 ✓。
（哪天在服务器上新增了文件夹，本机看不到它的设置 —— 那种情况要单独处理 ✓）

命令行：
    python -m app.foldersync --db <库> --init           # 加列 + 触发器（幂等）
    python -m app.foldersync --db <库> --dump           # 导出 JSON
    python -m app.foldersync --db <库> --apply <file>   # 合并进来
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger("eg2.foldersync")

FIELDS = ("id", "path", "owner_id", "visibility", "updated_at")


def _cols(con: sqlite3.Connection, table: str = "folders") -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def ensure_folder_timestamps(db: str | Path) -> list[str]:
    """给 folders 加 updated_at + 维护触发器（幂等）。"""
    con = sqlite3.connect(str(db), timeout=30)
    changes: list[str] = []
    try:
        cols = _cols(con)
        if not cols:
            return changes                      # 还没有 folders 表，等启动一次建表
        with con:
            if "updated_at" not in cols:
                con.execute("ALTER TABLE folders ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
                changes.append("folders.updated_at")
            if "owner_id" not in cols:
                con.execute("ALTER TABLE folders ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0")
                changes.append("folders.owner_id")
            if "visibility" not in cols:
                con.execute("ALTER TABLE folders ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'")
                changes.append("folders.visibility")
            cur = con.execute(
                "UPDATE folders SET updated_at = COALESCE(NULLIF(last_scan, 0), "
                "COALESCE(added_at, 0), 0) WHERE updated_at IS NULL OR updated_at = 0")
            if cur.rowcount:
                changes.append(f"回填 {cur.rowcount} 行")
            # 只有"设置"变了才刷新时间；last_scan 之类不算 ✓
            con.execute(
                """
                CREATE TRIGGER IF NOT EXISTS folders_bump_updated
                AFTER UPDATE ON folders
                WHEN (NEW.visibility IS NOT OLD.visibility)
                  OR (NEW.owner_id   IS NOT OLD.owner_id)
                  OR (NEW.config     IS NOT OLD.config)
                BEGIN
                    UPDATE folders SET updated_at =
                        CAST((julianday('now') - 2440587.5) * 86400.0 AS REAL)
                    WHERE id = NEW.id;
                END
                """)
            changes.append("触发器 folders_bump_updated")
        return changes
    finally:
        con.close()


def dump_folders(db: str | Path) -> list[dict]:
    con = sqlite3.connect(str(db), timeout=20)
    con.row_factory = sqlite3.Row
    try:
        cols = _cols(con)
        sel = ", ".join(f for f in FIELDS if f in cols)
        return [dict(r) for r in con.execute(f"SELECT {sel} FROM folders ORDER BY id")]
    finally:
        con.close()


def _newer(row: dict, cur: dict) -> bool:
    """对方这行是否该覆盖本行。

    规则：**更新的胜出**；**时间相同则取更严的（private 优先）** ——
    这一点很重要：原来"平手就保留本地"会让两台机器各留各的、永久不一致 ✗
    （实测过：同一秒里两边各改一次 → 合并后 PC=shared / 服务器=private ✓）。
    取"更严"是**对称**的，从任一方向算都得到同一个结果 ✓，而且偏安全 ✓。
    """
    new_t = float(row.get("updated_at") or 0)
    cur_t = float(cur.get("updated_at") or 0)
    if new_t != cur_t:
        return new_t > cur_t
    return (str(row.get("visibility")) == "private"
            and str(cur.get("visibility")) != "private")


def merge_folders(local: list[dict], remote: list[dict]) -> list[dict]:
    """按 id 取并集；同一文件夹，更新的设置胜出（平手取更严的）。"""
    out: dict[int, dict] = {}
    for row in local or []:
        if row.get("id") is not None:
            out[int(row["id"])] = dict(row)
    for row in remote or []:
        if row.get("id") is None:
            continue
        k = int(row["id"])
        cur = out.get(k)
        if cur is None or _newer(row, cur):
            out[k] = dict(row)
    return [out[k] for k in sorted(out)]


def apply_folder_settings(db: str | Path, rows: list[dict]) -> dict:
    """把设置合并进这个库：**只更新 visibility / owner_id / updated_at**；

    不新增、不删除、绝不改 path ✓。同名（同 id）只在对方更新时才覆盖。
    """
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    stat = {"updated": 0, "kept": 0, "missing": 0}
    try:
        cols = _cols(con)
        have = {int(r["id"]): dict(r) for r in con.execute("SELECT * FROM folders")}
        with con:
            for row in rows or []:
                if row.get("id") is None:
                    continue
                k = int(row["id"])
                cur = have.get(k)
                if cur is None:
                    stat["missing"] += 1          # 本库没有这个文件夹 → 不动它
                    continue
                if _newer(row, cur):
                    sets, vals = [], []
                    for f in ("visibility", "owner_id", "updated_at"):
                        if f in cols and f in row:
                            sets.append(f + "=?")
                            vals.append(row[f])
                    if sets:
                        con.execute(f"UPDATE folders SET {','.join(sets)} WHERE id=?",
                                    vals + [k])
                        # 关键：触发器会在上面这条改完设置后把 updated_at 刷成"现在" ✗，
                        # 于是"同步过来的时间戳"存不住 → 下次同步又会写 → 两边永久微差 ✗。
                        # 再补一条**只改 updated_at** 的更新：此时触发器条件不成立，
                        # 时间就定住了 ✓（实测：不补这一条，幂等测试会失败 ✓）
                        if "updated_at" in cols and "updated_at" in row:
                            con.execute("UPDATE folders SET updated_at=? WHERE id=?",
                                        (row["updated_at"], k))
                        stat["updated"] += 1
                    else:
                        stat["kept"] += 1
                else:
                    stat["kept"] += 1
        return stat
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="文件夹设置双向合并")
    ap.add_argument("--db", default=str(Path("data/index/index.db")))
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--apply", metavar="FILE")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")                     # noqa: F821

    if args.init:
        print("  " + ("、".join(ensure_folder_timestamps(args.db)) or "本来就到位"))
    if args.dump:
        print(json.dumps(dump_folders(args.db), ensure_ascii=False, indent=2))
    if args.apply:
        rows = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        print("  " + json.dumps(apply_folder_settings(args.db, rows), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
