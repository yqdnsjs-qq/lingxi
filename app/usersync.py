"""账号的双向合并（多人 P4-a）。

规则（项目主要求）：**两边取并集；同一个账号，谁的改动更新就用谁那一行。**
也就是说 PC 上审批过、服务器上又改过，最后两边都会变成"更新的那个状态"，
谁改都丢不了 ✓。

实现要点：
  · `users` 加一列 `updated_at`（改动时间）；用**触发器**维护 ——
    只在 state / role / pwd 真的变化时刷新，**登录( last_login )不算修改** ✓
    （否则"谁刚登录"就会把另一边的审批状态盖掉，那不是用户的本意）
  · 合并按 `name` 做并集，**绝不删除任何账号**（并集语义）
  · 同名比 `updated_at`：更晚的整行胜出（含密码哈希、角色、状态）
    时间相同/缺失 → 保留本地那份（保证确定性，重复跑结果一致）
  · `sessions` 与 `audit` **不参与合并**：会话是本机/本服务器的本地状态，
    同步过去会串号；审计各留各的

这个模块是纯逻辑 + 两个 SQL 操作，**不碰同步流程**（接线在 P4-b，单独验证）。
命令行（服务器上可以用它来 dump/apply）：
    python -m app.usersync --db <库> --init            # 加列 + 建触发器（幂等）
    python -m app.usersync --db <库> --dump            # 导出 JSON（含密码哈希）
    python -m app.usersync --db <库> --apply <file>    # 把 JSON 合并进来
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

log = logging.getLogger("eg2.usersync")

FIELDS = ("id", "name", "pwd", "role", "state", "created_at", "last_login", "updated_at")


def _cols(con: sqlite3.Connection) -> set[str]:
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(users)")}
    except sqlite3.Error:
        return set()


def ensure_updated_at(db: str | Path) -> list[str]:
    """加 updated_at 列 + 维护触发器（幂等）。返回实际做了哪些改动。"""
    con = sqlite3.connect(str(db), timeout=30)
    changes: list[str] = []
    try:
        cols = _cols(con)
        if not cols:
            raise RuntimeError("这个库里没有 users 表（先让服务启动一次建表）")
        with con:
            if "updated_at" not in cols:
                con.execute("ALTER TABLE users ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
                changes.append("users.updated_at")
            # 老数据回填：用 created_at 当改动时间
            cur = con.execute("UPDATE users SET updated_at = COALESCE(created_at, 0) "
                              "WHERE updated_at IS NULL OR updated_at = 0")
            if cur.rowcount:
                changes.append(f"回填 {cur.rowcount} 行 updated_at")
            # 触发器：只在 state / role / pwd 变化时刷新（登录不算），
            # 内层 UPDATE 只改 updated_at → WHEN 不成立 → 不会递归
            con.execute(
                """
                CREATE TRIGGER IF NOT EXISTS users_bump_updated
                AFTER UPDATE ON users
                WHEN (NEW.state IS NOT OLD.state)
                  OR (NEW.role  IS NOT OLD.role)
                  OR (NEW.pwd   IS NOT OLD.pwd)
                BEGIN
                    UPDATE users SET updated_at =
                        CAST((julianday('now') - 2440587.5) * 86400.0 AS REAL)
                    WHERE id = NEW.id;
                END
                """)
            changes.append("触发器 users_bump_updated")
        return changes
    finally:
        con.close()


def dump_users(db: str | Path) -> list[dict]:
    con = sqlite3.connect(str(db), timeout=20)
    con.row_factory = sqlite3.Row
    try:
        cols = _cols(con)
        sel = ", ".join(f for f in FIELDS if f in cols)
        rows = con.execute(f"SELECT {sel} FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def merge_users(local: list[dict], remote: list[dict]) -> list[dict]:
    """并集 + 同名取 updated_at 更晚的。纯函数，方便测试。"""
    out: dict[str, dict] = {}
    for row in local or []:
        if row.get("name"):
            out[str(row["name"])] = dict(row)
    for row in remote or []:
        name = str(row.get("name") or "")
        if not name:
            continue
        cur = out.get(name)
        if cur is None:
            out[name] = dict(row)
            continue
        if float(row.get("updated_at") or 0) > float(cur.get("updated_at") or 0):
            out[name] = dict(row)           # 更新的一方胜出（整行替换，含密码哈希）
    return sorted(out.values(), key=lambda r: (float(r.get("created_at") or 0),
                                               str(r.get("name") or "")))


def apply_users(db: str | Path, rows: list[dict]) -> dict:
    """把 rows 合并进这个库（同名只在对方更新时才覆盖）；**不删除任何账号**。

    返回 {"added": n, "updated": n, "kept": n}
    """
    con = sqlite3.connect(str(db), timeout=30)
    con.row_factory = sqlite3.Row
    stat = {"added": 0, "updated": 0, "kept": 0}
    try:
        cols = _cols(con)
        have = {str(r["name"]): dict(r) for r in
                con.execute("SELECT * FROM users").fetchall()}
        with con:
            for row in rows or []:
                name = str(row.get("name") or "")
                if not name:
                    continue
                cur = have.get(name)
                if cur is None:
                    # 注意：**不能带 id** —— 两边的自增 id 会撞车（实测报
                    # UNIQUE constraint failed: users.id）。账号的身份是 name，
                    # id 只是本库的局部编号，让目标库自己分配。
                    keys = [f for f in FIELDS if f in cols and f in row and f != "id"]
                    con.execute(
                        f"INSERT INTO users({','.join(keys)}) "
                        f"VALUES({','.join('?' * len(keys))})",
                        [row[k] for k in keys])
                    stat["added"] += 1
                    continue
                if float(row.get("updated_at") or 0) > float(cur.get("updated_at") or 0):
                    keys = [f for f in ("pwd", "role", "state", "updated_at")
                            if f in cols and f in row]
                    if keys:
                        con.execute(
                            f"UPDATE users SET {','.join(k + '=?' for k in keys)} "
                            f"WHERE name=?", [row[k] for k in keys] + [name])
                        # 同 foldersync：触发器会把 updated_at 刷成"现在" ✗，
                        # 再补一条只改 updated_at 的更新把它定回同步过来的值 ✓
                        if "updated_at" in cols and "updated_at" in row:
                            con.execute("UPDATE users SET updated_at=? WHERE name=?",
                                        (row["updated_at"], name))
                        stat["updated"] += 1
                    else:
                        stat["kept"] += 1
                else:
                    stat["kept"] += 1
        return stat
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="账号双向合并")
    ap.add_argument("--db", default=str(Path("data/index/index.db")))
    ap.add_argument("--init", action="store_true", help="加列 + 建触发器")
    ap.add_argument("--dump", action="store_true", help="导出 JSON")
    ap.add_argument("--apply", metavar="FILE", help="把 JSON 合并进来")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")                     # noqa: F821

    if args.init:
        print("  " + ("、".join(ensure_updated_at(args.db)) or "本来就到位"))
    if args.dump:
        print(json.dumps(dump_users(args.db), ensure_ascii=False, indent=2))
    if args.apply:
        rows = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        print("  " + json.dumps(apply_users(args.db, rows), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
