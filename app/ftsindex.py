"""FTS5 关键词检索通道（多人 ⑤ 知识库检索加强 —— 第一步 FTS5 + RRF）。

## 为什么不用"直接建在 text 上"的默认做法
中文没有空格，FTS5 默认的 `unicode61` 会把一整串汉字当成**一个** token
（"这是一段家谱记载" 只能整串匹配）✗；换成 `trigram` 又要求查询至少 3 个字，
「家谱」「报销」「张三」这种最常见的两字词**一条都查不到** ✗（实测：0 条）。

## 本模块的做法：**逐字切分 + 短语查询**
  · 入库：把正文切成"单字之间夹空格"（`unigrams()`），存进 `chunks.uni`
  · 查询：把查询词也切成单字，做成**短语**（`"家 谱"`）→ 要求这些字**相邻出现**
    ⇒ 语义上等价于**子串匹配** ✓，中英文都一样（`"2 0 2 4"` 能命中 "2024年"）
  · 实测（7.6M 字、4.9 万片段）：建索引 **1.8s**、库增大 **+33MB**、查询 **<1ms**
    （对照：trigram +171MB 且两字中文查不到 ✗；bigram +92MB 且同样查不到 ✗；
     老的 LIKE 全表扫描 **每次 65~90ms**，还要按关键词重复扫 ✗）
  · 排序用 bm25()（所以 detail=full、columnsize 保持默认 ✓，短语查询也要 full）

## 为什么不手工维护索引（contentless 表）
`content='chunks'` + 三个触发器 ⇒ 只要往 chunks 写入（`Store.add_chunks`、
`caption.py`）索引就自动同步 ✓，删文件也自动清理 ✓，不需要在每个写入点记得调一次
（那种约定迟早会漏 ✗）。代价是多存一份 `chunks.uni`（约正文大小的 2 倍，实测 +15MB 级别）。
"""
from __future__ import annotations

import logging
import re
import sqlite3

log = logging.getLogger("eg2.fts")

FTS_TABLE = "chunks_fts"
SPLIT_RE = re.compile(r"(\s+)")
# 全角/半角、大小写差异在 unicode61 里处理；这里只负责"逐字切开" ✓
TRIGGERS = (
    f"""CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
          INSERT INTO {FTS_TABLE}(rowid, uni) VALUES (new.id, new.uni);
        END""",
    f"""CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
          INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, uni)
          VALUES ('delete', old.id, old.uni);
        END""",
    f"""CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
          INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, uni)
          VALUES ('delete', old.id, old.uni);
          INSERT INTO {FTS_TABLE}(rowid, uni) VALUES (new.id, new.uni);
        END""",
)


def unigrams(text: str) -> str:
    """正文 → 逐字切分（空白保留为分隔）。空文本给空串。"""
    if not text:
        return ""
    out: list[str] = []
    for chunk in SPLIT_RE.split(text):
        if not chunk:
            continue
        if chunk.isspace():
            out.append(chunk)
            continue
        out.append(" ".join(chunk))
    return "".join(out)


def query_expr(q: str) -> str:
    """查询 → FTS5 MATCH 表达式：每个词做成单字短语，词之间 AND。

    · 短语 = 相邻出现 ⇒ 真正的子串匹配 ✓
    · 引号会被转义（用户可能输入引号/AND 之类）
    · 切不出词（纯标点）→ 返回空串，调用方直接跳过 ✓
    """
    parts: list[str] = []
    for chunk in SPLIT_RE.split((q or "").strip()):
        if not chunk or chunk.isspace():
            continue
        units = [c for c in chunk if not c.isspace()]
        if not units:
            continue
        # FTS5 短语里用双引号包起来，内部的双引号要写成两个
        phrase = " ".join(u.replace('"', '""') for u in units)
        parts.append(f'"{phrase}"')
    return " AND ".join(parts)


# ---------------------------------------------------------------------------
# 关键词候选（2026-10-16 修：长问句里的名字排不到前面，检索整句又匹配不到）
#
# 项目主实测：问「方六是张三的什么人」，回答居然说"资料里既没有『张三』，
# 也没有出现『方六』" ✗ —— 可是这两个名字**库里明明都有**（各 2~3 个片段 ✓）。
# 两个原因叠在一起：
#   ① 候选词按"整段 → 滑窗碎片"排，前 4 个是
#      「方六是张三的什么人 / 方六是磊 / 六是张三 / 是张三的」这种带连接词的碎片 ✗，
#      真正的名字排到第 15 位以后，被 `[:4]` 截掉 ✗
#   ② 全文通道拿**整句**去做子串匹配（9 个字连着出现）→ 必然 0 命中 ✗，
#      于是名字既没从向量通道稳稳捞到、也没有字面兜底
# 修法：① 用**停用字**把问句切成"实词块"（人名/术语通常整块连着）放在最前面 ✓
#       ② `search()` 里整句 0 命中时**自动拆词重试**，按"命中词数"排序 ✓
#          （同一个片段里同时出现「方六」和「张三」的排最前，模型才能答关系 ✓）
# 这份切词只此一处，filesearch 直接复用 —— 上次"两份实现只改一处"的坑不再踩 ✓
# ---------------------------------------------------------------------------
STOP_CHARS = set(
    "是谁的了吗呢哪什么怎样多少为在和我你他她它请问说讲告诉情况资料文件内容信息"
    "关于以及还有可以能不能一下介绍想要知道了人者之其该此那这们与及个把被让给"
    "对从到于就也都还很更最")
_STOP_TERMS = {
    "是谁", "是什么", "什么", "哪个", "哪些", "哪里", "哪儿", "怎么", "怎样", "怎么样",
    "多少", "为什么", "干啥", "吗", "呢", "介绍", "一下", "说说", "讲讲", "告诉",
    "情况", "资料", "文件", "内容", "信息", "关于", "可以", "能不能", "有没有",
    "想要", "知道", "了解",
}


def is_noise_term(w: str) -> bool:
    """纯疑问词/口头语 —— 别拿它去做字面匹配 ✓"""
    if w in _STOP_TERMS:
        return True
    return len(w) <= 3 and all(c in STOP_CHARS for c in w)


def _blocks(part: str) -> list[str]:
    """用停用字把一段话切成"实词块"（人名/术语通常整块连着 ✓）。

    「方六是张三的什么人」→ ['方六', '张三'] ✓
    「怎么备份数据库」    → ['备份数据库'] ✓
    名字里含停用字会被切开（如「李人杰」→ 李 / 杰），但滑窗碎片仍作兜底 ✓
    """
    out: list[str] = []
    cur: list[str] = []
    for ch in part:
        if ch in STOP_CHARS:
            if len(cur) >= 2:
                out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if len(cur) >= 2:
        out.append("".join(cur))
    return out


def query_keywords(q: str) -> list[str]:
    """从问题里挑出适合做字面匹配的关键词，**实词块优先** ✓。

    顺序：实词块（长的先）→ 整段 → 4/3/2 字滑窗碎片（兜底）
    只保留长度 2~16，并滤掉纯疑问词。
    """
    text = (q or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in SPLIT_RE.split(text) if p.strip()]
    blocks: list[str] = []
    for part in parts:
        blocks += _blocks(part)
    out: list[str] = []
    seen: set[str] = set()

    def _add(word: str) -> None:
        if (word and word not in seen and 2 <= len(word) <= 16
                and not is_noise_term(word)):
            seen.add(word)
            out.append(word)

    for word in sorted(blocks, key=lambda s: (-len(s), s)):      # 名字排最前 ✓
        _add(word)
    for part in parts:
        if 2 <= len(part) <= 12:
            _add(part)
        if len(part) > 2:                                        # 3、4 字的也拆 ✓
            for n in (4, 3, 2):
                for i in range(0, len(part) - n + 1):
                    _add(part[i:i + n])
    return out[:10]


def available(con: sqlite3.Connection) -> bool:
    try:
        con.execute(f"SELECT COUNT(*) FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH 'x'")
        return True
    except sqlite3.Error:
        return False


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _index_alive(con: sqlite3.Connection) -> bool:
    """索引到底建没建 —— 只能用**真查一次**来判断 ✓。

    踩过的坑：对 `content=` 外部内容表，`SELECT COUNT(*) FROM fts` 会被 SQLite
    直接从内容表回答（刚建完、索引还空着也返回 5 万 ✗），
    `SELECT COUNT(*) FROM (SELECT rowid FROM fts)` 同样不可靠 ✗。
    所以拿内容表里第一个 token 真 MATCH 一次：**0 条 = 索引是空的** ✓
    （这个坑让第一版 ensure() 跳过了 rebuild，导致所有查询都返回 0 条 ✗）
    """
    try:
        row = con.execute(
            "SELECT uni FROM chunks WHERE uni IS NOT NULL AND uni<>'' LIMIT 1").fetchone()
        if not row:
            return True                                          # 没有正文可索引，算"活着"
        token = str(row[0]).split()
        if not token:
            return True
        probe = token[0].replace('"', '""')
        n = con.execute(f"SELECT COUNT(*) FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?",
                        (f'"{probe}"',)).fetchone()[0]
        return int(n) > 0
    except sqlite3.Error:
        return False


def ensure(con: sqlite3.Connection, *, verbose: bool = False) -> dict:
    """幂等建好 FTS 通道：加列 → 建表 → 建触发器 → 回填 → 对账。

    · 第一次跑（或库是老的/从别人那拷来的）会做全量回填 + rebuild（几秒）
    · 之后每次启动只是几次查询，毫秒级 ✓
    · **任何异常都不往外抛**：检索加强属于"锦上添花"，
      绝不能因为 FTS 建不起来就让整个服务起不来 ✗（调用方自己兜底）
    """
    out: dict = {"ok": False, "backfilled": 0, "rebuilt": False, "rows": 0}
    try:
        if "chunks" not in {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            return out                                          # 空库/老库还没 chunks 表
        if "uni" not in _cols(con, "chunks"):
            con.execute("ALTER TABLE chunks ADD COLUMN uni TEXT")
            con.commit()
        todo = con.execute("SELECT COUNT(*) FROM chunks WHERE uni IS NULL").fetchone()[0]
        if todo:
            rows = con.execute(
                "SELECT id, text FROM chunks WHERE uni IS NULL").fetchall()
            con.executemany("UPDATE chunks SET uni=? WHERE id=?",
                            ((unigrams(t or ""), int(i)) for i, t in rows))
            con.commit()
            out["backfilled"] = len(rows)
            if verbose:
                log.info("FTS 回填 %d 条片段的逐字文本", len(rows))
        existed = bool(con.execute("SELECT 1 FROM sqlite_master WHERE name=?",
                                  (FTS_TABLE,)).fetchone())
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
            f"uni, content='chunks', content_rowid='id', tokenize=\"unicode61\", "
            f"detail=full)")
        for sql in TRIGGERS:
            con.execute(sql)
        con.commit()
        # 什么时候必须 rebuild：表是新建的 / 刚补过 uni / **真查一次发现索引是空的** ✓
        if (not existed) or out["backfilled"] or not _index_alive(con):
            con.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('rebuild')")
            con.commit()
            out["rebuilt"] = True
        out["rows"] = int(con.execute(
            f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0])
        out["chunks"] = int(con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        out["alive"] = _index_alive(con)
        out["ok"] = bool(out["alive"])
        try:
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('fts_built',?)",
                        (f"rows={out['rows']} rebuilt={int(out['rebuilt'])}",))
            con.commit()
        except sqlite3.Error:
            pass                                                # 没 meta 表也无所谓
    except sqlite3.Error as exc:
        log.warning("FTS 通道建立失败（检索退化成原来的向量 + LIKE）：%s", exc)
        out["error"] = str(exc)
    return out


def search(con: sqlite3.Connection, q: str, limit: int = 60,
           folder_ids: list[int] | None = None,
           exclude_kinds: tuple | None = None) -> list[dict]:
    """关键词命中：返回 [{chunk_id, file_id, folder_id, snippet, score}]（按 bm25）。

    score 归一化到 0~1（越大越相关），方便和向量分数一起进 RRF / 展示 ✓

    **整句 0 命中时自动拆词重试** ✓：像「方六是张三的什么人」这种问句，
    整句在正文里不可能连着出现，不拆就永远 0 命中 ✗ —— 拆开以后
    「方六」「张三」都能命中，而且**同一片段命中词数多的排前面** ✓
    （两条信息在同一个片段里，模型才答得出"什么关系" ✓）。
    整句能命中就不拆（保持"精确优先"的原有语义 ✓）。
    """
    expr = query_expr(q)
    if not expr:
        return []
    seen: dict[int, dict] = {}

    def _run(match_expr: str, term: str) -> int:
        sql = (f"SELECT c.rowid AS chunk_id, c.file_id, f.folder_id, c.kind, c.text, "
               f"       bm25({FTS_TABLE}) AS rank "
               f"  FROM {FTS_TABLE} "
               f"  JOIN chunks c ON c.id = {FTS_TABLE}.rowid "
               f"  JOIN files f ON f.id = c.file_id "
               f" WHERE {FTS_TABLE} MATCH ? ")
        args: list = [match_expr]
        if folder_ids:
            sql += " AND f.folder_id IN (" + ",".join("?" * len(folder_ids)) + ")"
            args += [int(x) for x in folder_ids]
        if exclude_kinds:
            sql += " AND c.kind NOT IN (" + ",".join("?" * len(exclude_kinds)) + ")"
            args += list(exclude_kinds)
        sql += " ORDER BY rank LIMIT ?"
        args.append(int(max(1, min(limit, 500))))
        try:
            rows = con.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            log.debug("FTS 查询失败：%s", exc)
            return 0
        for r in rows:
            rank = float(r["rank"] or 0)
            # bm25 在 FTS5 里是**越小越相关**（负数），0~1 归一化后越大越相关 ✓
            score = 1.0 / (1.0 + max(0.0, rank)) if rank >= 0 else min(1.0, -rank / 10.0)
            cid = int(r["chunk_id"])
            cur = seen.get(cid)
            if cur is None:
                seen[cid] = {
                    "chunk_id": cid, "file_id": int(r["file_id"]),
                    "folder_id": int(r["folder_id"] or 0), "kind": r["kind"],
                    "snippet": (r["text"] or "").strip()[:600],
                    "score": round(score, 4),
                    "hit_terms": 1, "matched": term,
                }
            else:
                cur["hit_terms"] += 1          # 一个片段被越多关键词命中越相关 ✓
                if score > cur["score"]:
                    cur["score"] = round(score, 4)
        return len(rows)

    if _run(expr, q):                          # ① 整句先试（精确）
        out = list(seen.values())
    else:                                      # ② 拆词重试（召回）
        for term in query_keywords(q)[:6]:
            _run(query_expr(term), term)
        out = list(seen.values())
    out.sort(key=lambda r: (-int(r.get("hit_terms") or 1), -float(r.get("score") or 0)))
    return out[:int(max(1, min(limit, 500)))]


def stats(con: sqlite3.Connection) -> dict:
    try:
        rows = con.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0]
        return {"ok": True, "rows": int(rows), "table": FTS_TABLE}
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
