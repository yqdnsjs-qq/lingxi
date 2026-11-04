"""文件名 / 目录的 FTS5 通道（3.5.14 起）。

## 为什么要它
搜索里"文件名包含关键词"这一路（`literal_search`）一直是**整表 LIKE 扫描**：
62 万行、2.3 GB 的表，命中稀有词要扫完（实测 1~2 秒），扫路径更贵（7.8 秒）。
而用户搜「报纸」「合同」「compiler_bisector」这类词，要的正是**文件名精确命中**。

## 怎么切词：中文逐字、英文整词（`tokens()`）
· 中文没有空格，FTS5 的 `trigram` 要求至少 3 个字（「报纸」查不到 ✗，`ftsindex.py`
  里踩过），所以中文必须**逐字切开**再按**短语**查（`"报 纸"` = 两字相邻）✓
· 英文/数字反过来：**不能**逐字。`compiler_bisector` 逐字会变成 18 个单字母 token，
  而单字母在 62 万条里几乎每条都有 → FTS5 求值短语要扫几十万条，
  实测 **1.2 秒**才确认唯一那一条 ✗（比 LIKE 还慢）。按整词留以后只要 **1~5 ms** ✓

## 为什么不用 bm25 排序
本模块不排 bm25。实测 62 万文档的库上：「报纸」命中 6.2 万条，
`ORDER BY bm25()` 要 **250 ms**；「jpg」这种命中全部 62 万条的要 **1.6 秒** ✗ ——
比它要替代的 LIKE 还慢。改成 **rowid 倒序取候选**（= 新文件优先）再在 Python 里
按「完全相同 > 前缀 > 包含 > 只在目录里」重排：**5~20 ms**，结果更符合直觉 ✓。

## 怎么保持同步（不吃触发器，显式维护 + 自愈）
· `Store.upsert_file` / `delete_paths` / `remove_folder` / `reset` 里显式增删
· 统计对不上（外部脚本直接写了 files 表）→ 下次启动自动全量重建
· 目录被改写（`remap_paths.py` 只改 dir，行数不变）→ 记 `name_fts_dirty` 标志，同样重建
· 切词规则一变（`FORMAT` 加一）→ 老索引自动判定为过期，下次启动重建 ✓
· 重建 62 万条约 3 分钟，在**后台线程**里做，不挡启动、也不挡搜索
  （没建好之前 `literal_search` 自动退回老的 LIKE 路径 ✓）
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Iterable

log = logging.getLogger("eg2.namefts")

NAME_FTS = "files_fts"
DIRTY_KEY = "name_fts_dirty"
FMT_KEY = "name_fts_fmt"
FORMAT = "2"          # 切词规则版本：中文逐字 + 英文整词（1 = 全逐字，已废弃）

# 一条 SQL 里取回的列：故意**不**包含 name_vec（900 MB 的 BLOB，取出来白费）
_COLS = ("id", "folder_id", "path", "name", "dir", "ext", "kind", "size",
         "mtime", "status", "note", "n_chunks")

STRIP = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS {t} USING fts5("
    "name, dir, tokenize='unicode61', detail=full)"
).format(t=NAME_FTS)


# --------------------------------------------------------------------- 切词
def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF or 0x3040 <= o <= 0x30FF)   # 汉字 + 假名


def tokens(text: str) -> list[str]:
    """文件名/路径 → 词序列。**中文逐字切、英文按整词留**。

    为什么不全逐字：中文没有空格，"逐字 + 短语"是唯一能命中两字词的办法 ✓；
    但英文/数字如果也逐字，`compiler_bisector` 会变成 18 个单字token，
    而这些单字母在 62 万条里几乎每条都有 —— FTS5 短语求值要扫几十万条，
    实测 **1.2 秒**才能确认唯一那一条 ✗（比要替代的 LIKE 还慢）。
    按整词留以后：`compiler` / `bisector` 都是稀有词，**1~5 毫秒** ✓；
    `bisector` 单独搜也能命中（下划线当分隔符，正合直觉）。
    """
    out: list[str] = []
    word: list[str] = []
    for ch in text or "":
        if ch.isascii() and ch.isalnum():
            word.append(ch)
            continue
        if word:
            out.append("".join(word))
            word.clear()
        if _is_cjk(ch):
            out.append(ch)
    if word:
        out.append("".join(word))
    return out


def token_text(text: str) -> str:
    """建索引用：词之间留空格（unicode61 就按空格分成 token）✓"""
    return " ".join(tokens(text))


def query_expr(q: str) -> str:
    """查询 → MATCH 表达式。一个词 = 一段**相邻短语**，词之间 AND。

    用 `{name dir}:` 限定在这两列里找，避免日后加列时语义漂移。
    切不出词（纯标点/空白）→ 返回空串，调用方直接跳过 ✓
    """
    parts: list[str] = []
    for word in (q or "").split():
        units = tokens(word)
        if not units:
            continue
        phrase = " ".join(u.replace('"', '""') for u in units)
        parts.append(f'{{name dir}}:"{phrase}"')
    return " AND ".join(parts)


# --------------------------------------------------------------------- 结构
def has_table(con: sqlite3.Connection) -> bool:
    try:
        return bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (NAME_FTS,)).fetchone())
    except sqlite3.Error:
        return False


def create(con: sqlite3.Connection) -> None:
    con.execute(STRIP)
    con.commit()


def count(con: sqlite3.Connection) -> int:
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {NAME_FTS}").fetchone()[0])
    except sqlite3.Error:
        return -1


def files_count(con: sqlite3.Connection) -> int:
    try:
        return int(con.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    except sqlite3.Error:
        return 0


def _meta_get(con: sqlite3.Connection, key: str) -> str:
    try:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


def _meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    try:
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
    except sqlite3.Error:
        pass


def mark_dirty(con: sqlite3.Connection, why: str = "") -> None:
    """记一笔"这份索引不可信了"，下次 ensure 会重建（目录被批量改写的场景）。"""
    if not has_table(con):
        return
    _meta_set(con, DIRTY_KEY, str(why or "1"))
    try:
        con.commit()
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------- 建 / 重建
def rebuild(con: sqlite3.Connection, *, batch: int = 5000,
            verbose: bool = False) -> dict:
    """全量重建（先清空）。62 万条约 25 秒，只在必要时调用。"""
    t0 = time.time()
    create(con)
    con.execute(f"DELETE FROM {NAME_FTS}")
    con.commit()
    done = 0
    cur = con.execute("SELECT id, name, dir FROM files")
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            break
        con.executemany(
            f"INSERT INTO {NAME_FTS}(rowid, name, dir) VALUES(?,?,?)",
            ((int(r[0]), token_text(r[1] or ""), token_text(r[2] or "")) for r in rows))
        con.commit()
        done += len(rows)
        if verbose and done % 100_000 < batch:
            log.info("文件名 FTS 已写入 %d 条…", done)
    con.execute(f"INSERT INTO {NAME_FTS}({NAME_FTS}) VALUES('optimize')")
    con.commit()
    _meta_set(con, DIRTY_KEY, "")
    _meta_set(con, FMT_KEY, FORMAT)
    con.commit()
    took = time.time() - t0
    out = {"ok": True, "rows": count(con), "files": files_count(con),
           "took": round(took, 1)}
    log.info("文件名 FTS 重建完成：%d 条，%.1f 秒", out["rows"], took)
    return out


def need_rebuild(con: sqlite3.Connection) -> tuple[bool, str]:
    if not has_table(con):
        return True, "表不存在"
    if _meta_get(con, DIRTY_KEY):
        return True, "标记为脏（目录被批量改写）"
    fmt = _meta_get(con, FMT_KEY)
    if fmt != FORMAT:
        return True, f"切词规则版本不对（库内 {fmt or '空'} / 现在 {FORMAT}）"
    n_fts, n_files = count(con), files_count(con)
    if n_fts != n_files:
        return True, f"行数不一致（fts {n_fts} / files {n_files}）"
    return False, ""


def ensure(con: sqlite3.Connection, *, force: bool = False,
           verbose: bool = False) -> dict:
    """幂等：建表 → 需要时全量重建。**异常不外抛**（检索退化成 LIKE 即可）✓"""
    out: dict = {"ok": False, "rebuilt": False, "rows": 0, "files": 0, "reason": ""}
    try:
        create(con)
        out["files"] = files_count(con)
        if out["files"] == 0:
            out["ok"] = True                     # 空库：没东西可索引，也算好了
            out["rows"] = count(con)
            return out
        bad, why = (True, "force") if force else need_rebuild(con)
        out["reason"] = why
        if bad:
            got = rebuild(con, verbose=verbose)
            out.update(rebuilt=True, rows=got["rows"], took=got["took"])
        else:
            out["rows"] = count(con)
        out["ok"] = out["rows"] == out["files"] or out["rows"] > 0
    except sqlite3.Error as exc:
        log.warning("文件名 FTS 通道建立失败（检索退回 LIKE）：%s", exc)
        out["error"] = str(exc)
    return out


# --------------------------------------------------------------------- 写
def upsert(con: sqlite3.Connection, file_id: int, name: str, dir_: str) -> bool:
    """单条同步（insert/update 都走这里）。

    不查 sqlite_master（每文件一次太浪费），表不在时**静默失败** ✓
    """
    try:
        con.execute(f"INSERT OR REPLACE INTO {NAME_FTS}(rowid, name, dir) VALUES(?,?,?)",
                    (int(file_id), token_text(name or ""), token_text(dir_ or "")))
        return True
    except sqlite3.Error as exc:
        log.debug("文件名 FTS 单条写入失败（忽略）：%s", exc)
        return False


def delete_ids(con: sqlite3.Connection, ids: Iterable[int]) -> int:
    ids = [int(x) for x in ids if x is not None]
    if not ids or not has_table(con):
        return 0
    n = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        con.execute(f"DELETE FROM {NAME_FTS} WHERE rowid IN ({marks})", chunk)
        n += len(chunk)
    return n


def clear(con: sqlite3.Connection) -> None:
    if has_table(con):
        con.execute(f"DELETE FROM {NAME_FTS}")


# --------------------------------------------------------------------- 查
def search(con: sqlite3.Connection, needle: str, top_k: int = 30,
           folder_ids: list[int] | None = None,
           exts: list[str] | None = None) -> list[dict[str, Any]]:
    """文件名/目录包含 needle 的文件，按「越像用户想要」排序返回。

    取候选：rowid 倒序（新文件优先）+ 上限 800~5000 条 —— 实测 5~20 ms；
    再在 Python 里按 完全相同 > 前缀 > 文件名包含 > 只在目录里 重排 ✓
    """
    q = (needle or "").strip()
    expr = query_expr(q)
    if not q or not expr or not has_table(con):
        return []
    # 候选上限：取够用就行，但**别卡得太死** ——
    # 实测「报纸」命中 312 条（48 个文件名 + 265 个目录），早先上限 200 时
    # 最老的那批（火炬报，1999~2014 年）被截掉，首条直接变成别的文件 ✗
    # 现在下限 800、上限 5000：常见词能全取到，病态词（"jpg"）仍只取最新的 5000 条 ✓
    limit = int(min(max(int(top_k) * 30, 800), 5000))
    sql = (f"SELECT {', '.join('f.' + c for c in _COLS)} "
           f"  FROM {NAME_FTS} JOIN files f ON f.id = {NAME_FTS}.rowid "
           f" WHERE {NAME_FTS} MATCH ?")
    args: list = [expr]
    if folder_ids:
        sql += " AND f.folder_id IN (" + ",".join("?" * len(folder_ids)) + ")"
        args += [int(x) for x in folder_ids]
    if exts:
        sql += " AND f.ext IN (" + ",".join("?" * len(exts)) + ")"
        args += [str(e).lower() for e in exts]
    sql += f" ORDER BY {NAME_FTS}.rowid DESC LIMIT ?"
    args.append(limit)
    try:
        rows = con.execute(sql, args).fetchall()
    except sqlite3.Error as exc:
        log.debug("文件名 FTS 查询失败：%s", exc)
        return []
    ql = q.lower()

    def rank_key(row) -> tuple:
        name = str(row["name"] or "")
        nl = name.lower()
        if nl == ql:
            cls = 0
        elif nl.startswith(ql):
            cls = 1
        elif ql in nl:
            cls = 2
        else:                                     # 只在目录里命中
            cls = 3
        return (cls, len(name), -float(row["mtime"] or 0))

    rows = sorted(rows, key=rank_key)[:int(top_k)]
    return [{
        "id": int(r["id"]), "folder_id": r["folder_id"], "path": r["path"],
        "name": r["name"], "dir": r["dir"], "ext": r["ext"], "kind": r["kind"],
        "size": r["size"], "mtime": r["mtime"], "status": r["status"],
        "note": r["note"], "n_chunks": r["n_chunks"],
        "score": 0.0, "snippet": "",
    } for r in rows]


def stats(con: sqlite3.Connection) -> dict:
    bad, why = need_rebuild(con)
    return {"ok": has_table(con) and not bad, "rows": count(con),
            "files": files_count(con), "table": NAME_FTS, "reason": why}
