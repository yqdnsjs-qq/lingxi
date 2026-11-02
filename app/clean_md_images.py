# -*- coding: utf-8 -*-
"""清掉知识库片段里的 markdown 图片语法。

问题
----
剪藏文章里，图片都以这种形式写在正文里：
    ![c09f8034a7dfc47313bf116a5053c16f_MD5.jpg](../_resources/c09f8034a7dfc47313bf116a5053c16f_MD5.jpg)

它们是**链接，不是内容**。但因为在 .md 正文里，属于 text 片段，
于是：
  · 检索命中它们 → 占满上下文
  · 模型把这一堆链接原样抄进回答 → 又长又慢又没用

处理
----
把 `![alt](路径)` 从片段文字里删掉，只保留真正可读的正文。
**不动向量**（向量是建索引时算好的，保留），所以语义搜索不受影响。

用法：
    venv\\Scripts\\python.exe app\\clean_md_images.py            # 演练
    venv\\Scripts\\python.exe app\\clean_md_images.py --apply    # 执行
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
import json
import re
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "index" / "index.db"

# ![任意alt](任意路径)  —— alt 里可能有 ]、路径里可能有 )
IMG_RE = re.compile(r"!\[[^\]]{0,200}\]\([^)\n]{0,400}\)")
# Obsidian 的嵌入语法 ![[文件]]
EMBED_RE = re.compile(r"!\[\[[^\]]{0,200}\]\]")
# 光秃秃的图片文件名行（形如  a1b2c3d4_MD5.jpg）
BARE_RE = re.compile(r"^[\w\-]{8,64}_MD5\.(?:jpe?g|png|gif|webp)\s*$", re.M)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    kb = []
    for r in conn.execute("SELECT id, path, config FROM folders"):
        try:
            cfg = json.loads(r["config"]) if r["config"] else {}
        except (ValueError, TypeError):
            cfg = {}
        if cfg.get("use_kb"):
            kb.append(int(r["id"]))
    if not kb:
        print("  没有标为「知识库」的文件夹")
        return 1
    ph = ",".join("?" * len(kb))
    print(f"  知识库文件夹: {kb}")

    rows = conn.execute(
        f"SELECT id, text FROM chunks WHERE text <> '' AND file_id IN "
        f"(SELECT id FROM files WHERE folder_id IN ({ph}))", kb).fetchall()
    print(f"  有文字的片段: {len(rows)}")

    changed, shrunk, emptied = [], 0, 0
    for r in rows:
        old = r["text"] or ""
        new = IMG_RE.sub(" ", old)
        new = EMBED_RE.sub(" ", new)
        new = BARE_RE.sub(" ", new)
        new = re.sub(r"[ \t]{2,}", " ", new)
        new = re.sub(r"\n{3,}", "\n\n", new).strip()
        if new != old:
            changed.append((r["id"], new))
            shrunk += len(old) - len(new)
            if len(new) < 40:
                emptied += 1

    print(f"  需要改写的片段: {len(changed)}")
    print(f"  共减少字符    : {shrunk:,}")
    print(f"  改完不足 40 字（等于作废）: {emptied}")
    print()

    if changed:
        print("  样例（前 2 条改写后的开头）：")
        for cid, txt in changed[:2]:
            print(f"    · {txt[:100]}")
        print()

    if not args.apply:
        print("  （演练模式，没有改动。确认后加 --apply 执行）")
        conn.close()
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = DB.with_name(f"index-backup-{stamp}.db")
    print(f"  备份 → {bak.name} …")
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(bak)
    src.backup(dst)
    dst.close()
    src.close()
    print(f"  备份完成（{bak.stat().st_size / 1e6:.0f} MB）")

    with conn:
        for cid, txt in changed:
            # 太短的直接清空，免得又变成"文件名当正文"那种垃圾
            conn.execute("UPDATE chunks SET text=? WHERE id=?",
                         (txt if len(txt) >= 40 else "", cid))
        conn.execute(
            f"UPDATE files SET n_chunks=(SELECT COUNT(*) FROM chunks "
            f"WHERE file_id=files.id AND text<>'') "
            f"WHERE folder_id IN ({ph})", kb)

    left = conn.execute(
        f"SELECT COUNT(*) c, SUM(LENGTH(text)) s FROM chunks "
        f"WHERE text<>'' AND file_id IN "
        f"(SELECT id FROM files WHERE folder_id IN ({ph}))", kb).fetchone()
    print(f"  ✓ 完成。可检索片段 {left['c']} 个，共 {left['s'] or 0:,} 字")
    print(f"  想还原：把 {bak.name} 拷回 index.db（先停服务）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
