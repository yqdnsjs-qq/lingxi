"""多用户 P2：数据归属与可见性（表结构迁移 + 权限查询）。

设计见 docs/多人使用设计.md。要点：
  · `owner_id = 0` 表示**共享库**（管理员维护，所有登录用户可读）
  · `owner_id > 0` 表示某个用户的私有文件夹
  · `folders.visibility`：`shared`（共享库可见）/ `private`（只有 owner 可见）
  · 例外用 `folder_acl(folder_id, user_id, can_read)` 指定给某些人
  · 迁移**幂等**：加列前先看 PRAGMA，重复跑不会出错；老数据一律归共享库
    （所以升级后表现不变 —— 现有 43819 条全部 owner_id=0 / visibility=shared）

命令行（便于先在有备份的副本上试）：
    python -m app.multiuser --db <库文件> --check     # 只看现状，不改
    python -m app.multiuser --db <库文件> --apply     # 执行迁移
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger("eg2.multiuser")

SHARED = 0
VISIBILITIES = ("shared", "private")

# 表名 → 建表语句（新表；老表用 ALTER 加列）
_NEW_TABLES = {
    "folder_acl": """
        CREATE TABLE IF NOT EXISTS folder_acl (
            folder_id INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            can_read  INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (folder_id, user_id)
        )""",
    "chat_history": """
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            q TEXT, answer TEXT, sources_json TEXT,
            engine TEXT, elapsed REAL
        )""",
}

_NEW_COLUMNS = {
    "folders": [
        ("owner_id", "INTEGER NOT NULL DEFAULT 0"),
        ("visibility", "TEXT NOT NULL DEFAULT 'shared'"),
        ("index_state", "TEXT NOT NULL DEFAULT 'ready'"),
        # 用途类别（2026-10-17 项目主要求）：find/photo/kb/media/all，
        # 空串 = 未分类（**老数据就是这个** → 行为完全不变 ✓）
        ("category", "TEXT NOT NULL DEFAULT ''"),
    ],
    "files": [
        ("owner_id", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def inspect(db: str | Path) -> dict:
    con = sqlite3.connect(str(db), timeout=30)
    try:
        out: dict = {"tables": sorted(_tables(con)), "columns": {}}
        for t in ("folders", "files"):
            out["columns"][t] = sorted(_cols(con, t))
        for t in ("folders", "files", "users"):
            if t in out["tables"]:
                out[t + "_count"] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if "owner_id" in out["columns"].get("folders", []):
            out["owners"] = dict(con.execute(
                "SELECT owner_id, COUNT(*) FROM folders GROUP BY owner_id").fetchall())
        if "visibility" in out["columns"].get("folders", []):
            out["visibility"] = dict(con.execute(
                "SELECT visibility, COUNT(*) FROM folders GROUP BY visibility").fetchall())
        return out
    finally:
        con.close()


def ensure_schema(db: str | Path) -> list[str]:
    """幂等迁移。返回这次实际做了哪些改动（空列表 = 本来就到位）。"""
    con = sqlite3.connect(str(db), timeout=30)
    changes: list[str] = []
    try:
        tables = _tables(con)
        with con:
            for table, cols in _NEW_COLUMNS.items():
                if table not in tables:
                    continue
                have = _cols(con, table)
                for name, decl in cols:
                    if name not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                        changes.append(f"{table}.{name}")
            for name, ddl in _NEW_TABLES.items():
                if name not in tables:
                    changes.append(f"table:{name}")
                con.execute(ddl)
            con.execute("CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_folders_owner ON folders(owner_id)")

        # 回填 files.owner_id，让它跟所属文件夹一致（幂等；只改不一致的行）
        fcols, flcols = _cols(con, "folders"), _cols(con, "files")
        if "owner_id" in fcols and "owner_id" in flcols and "folder_id" in flcols:
            with con:
                cur = con.execute(
                    "UPDATE files SET owner_id = COALESCE((SELECT f.owner_id FROM folders f "
                    "WHERE f.id = files.folder_id), 0) "
                    "WHERE owner_id <> COALESCE((SELECT f.owner_id FROM folders f "
                    "WHERE f.id = files.folder_id), 0)")
                if cur.rowcount:
                    changes.append(f"files.owner_id 回填 {cur.rowcount} 行")
        return changes
    finally:
        con.close()


# ---------------- 权限查询（P2-b 起被业务接口使用）----------------
def visible_folder_ids(db: str | Path, user: dict | None) -> list[int] | None:
    """这个用户能看到的文件夹 id。

    返回 None 表示**不限制**（管理员）；返回 [] 表示什么都看不到。

    规则（**共享与私有严格按管理员设置走** —— 这是 2026-10-16 项目主纠正的）：
      · 自己的私有文件夹 ✓
      · 共享库（owner_id=0）+ visibility='shared' ✓
      · `folder_acl` 里给了读权限的 ✓
      · **没登录（uid=0）只看共享库** ✗（不能"认领" owner_id=0 的私有文件夹 —— 实测泄漏过）

    ⚠ 曾经加过一条"标为知识库 → 对所有登录用户可见"，**已经删掉** ✗：
    那样会让管理员明确设成**私有**的文件夹也对所有人可见、连私有内容都能被搜到 ✗
    （项目主实测抓出来的）。"共享"就该只由 `visibility` 决定 ✓；
    要给普通用户用知识库，管理员把那个文件夹点成**共享**即可 ✓。
    """
    if user and user.get("role") == "admin":
        return None
    uid = int((user or {}).get("id") or 0)
    con = sqlite3.connect(str(db), timeout=20)
    con.row_factory = sqlite3.Row
    try:
        if "owner_id" not in _cols(con, "folders"):
            return None                                  # 还没迁移 → 不限制（老行为）
        rows = con.execute(
            "SELECT id, owner_id, visibility FROM folders").fetchall()
        acl = {int(r[0]) for r in con.execute(
            "SELECT folder_id FROM folder_acl WHERE user_id = ? AND can_read = 1",
            (uid,))}
        out: list[int] = []
        for r in rows:
            fid = int(r["id"])
            owner = int(r["owner_id"] or 0)
            vis = (r["visibility"] or "shared")
            # 注意：uid=0（匿名）绝不能被 owner_id=0 的规则"认领" ✗（实测泄漏过）
            if uid and owner == uid and owner != 0:
                out.append(fid)                      # 自己的
            elif vis == "shared" and (uid or owner == 0):
                # ★ 2026-10-21 修：**shared 就对所有登录用户可见**，不再要求 owner=0。
                #   之前写成 `owner == 0 and vis == "shared"`，把所有文件夹归给管理员之后，
                #   管理员标成"共享"的知识库对普通用户全部失效 ✗
                #   匿名（uid=0）仍然只看 owner=0 的公共库（保持原来的防泄漏）✓
                out.append(fid)
            elif fid in acl:
                out.append(fid)                      # 点名共享给我的
        return out
    finally:
        con.close()


def can_read_folder(db: str | Path, user: dict | None, folder_id: int) -> bool:
    if user and user.get("role") == "admin":
        return True
    ids = visible_folder_ids(db, user)
    return True if ids is None else int(folder_id) in ids


def writable_folder_ids(db: str | Path, user: dict | None) -> list[int] | None:
    """这个用户**能改**的文件夹 id（写路径 403 用，见 docs/多人使用设计.md §1）。

    返回 None 表示**不限制**（管理员 / 还没迁移的老库）；返回 [] 表示一个都不能改。

    规则比"可见"严得多 ✓：
      · 共享库（owner_id=0）对普通用户**只读** ✗ —— 界面能看，不能改
      · 别人的私有文件夹当然不能改 ✗
      · **匿名（uid=0）一个都不能改** ✓（和 visible_folder_ids 同一个坑：
        不排除 uid=0 的话，owner_id=0 的行会被匿名"认领"）
      · 自己的私有文件夹（owner_id=自己）能改 ✓
    folder_acl 目前只有 can_read（读例外），没有写例外 —— 所以写权限只认 owner ✓。
    """
    if user and user.get("role") == "admin":
        return None
    uid = int((user or {}).get("id") or 0)
    con = sqlite3.connect(str(db), timeout=20)
    try:
        if "owner_id" not in _cols(con, "folders"):
            return None                                  # 还没迁移 → 不限制（老行为）
        if uid == 0:
            return []                                    # 匿名：只读，什么都不给改 ✓
        rows = con.execute("SELECT id FROM folders WHERE owner_id = ?", (uid,)).fetchall()
        return [int(r[0]) for r in rows]
    finally:
        con.close()


def can_write_folder(db: str | Path, user: dict | None, folder_id: int) -> bool:
    """只有 owner（或管理员）能改；共享库(owner_id=0)对普通用户**只读**。"""
    if user and user.get("role") == "admin":
        return True
    uid = int((user or {}).get("id") or 0)
    con = sqlite3.connect(str(db), timeout=20)
    try:
        row = con.execute("SELECT owner_id FROM folders WHERE id=?", (folder_id,)).fetchone()
        if not row:
            return False
        return int(row[0]) == uid and uid != 0
    finally:
        con.close()


def set_visibility(db: str | Path, folder_id: int, visibility: str) -> None:
    if visibility not in VISIBILITIES:
        raise ValueError("visibility 只能是 shared / private")
    con = sqlite3.connect(str(db), timeout=20)
    try:
        with con:
            con.execute("UPDATE folders SET visibility=? WHERE id=?",
                        (visibility, folder_id))
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="多用户表结构迁移与检查")
    ap.add_argument("--db", default=str(Path("data/index/index.db")))
    ap.add_argument("--check", action="store_true", help="只看现状")
    ap.add_argument("--apply", action="store_true", help="执行迁移")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")                     # noqa: F821

    if args.check or not args.apply:
        info = inspect(args.db)
        print(f"  库: {args.db}")
        for k, v in info.items():
            print(f"  {k}: {v}")
        return 0

    changes = ensure_schema(args.db)
    print("  迁移完成：" + ("、".join(changes) if changes else "本来就到位（无改动）"))
    info = inspect(args.db)
    for k in ("owners", "visibility", "files_count", "folders_count"):
        if k in info:
            print(f"  {k}: {info[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
