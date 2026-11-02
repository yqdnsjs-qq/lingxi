"""清掉知识库文件夹里的噪声片段（URL 编码串、图片链接参数等）。

为什么需要
----------
实测发现：微信剪藏的 markdown 里，有很大一块内容是图片链接，
比如：
    fxkjd2%2FkvYy3U%2F%2FPGaMj%2B6OIPQja2FUZb...&exptype=time_recommend&fasttmpl_flag=0

这些被当成"正文"切成了片段。检索命中它们、喂给模型，
模型只能说"资料里没有提到" —— 真正有用的正文反而没被检索到。

这是个**数据清洗**，不改代码、不需要重启，
所以可以在索引重建期间安全执行。

用法：
    venv\\Scripts\\python.exe app\\clean_kb_chunks.py            # 演练
    venv\\Scripts\\python.exe app\\clean_kb_chunks.py --apply    # 真删
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
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "index" / "index.db"
sys.path.insert(0, str(ROOT))

import re                                       # noqa: E402


def is_junk(text: str) -> bool:
    """只认「明确的垃圾」，宁漏勿错。

    比 app.filesearch._kb_is_noise 保守得多，因为那个版本把
    markdown 表格也当成噪声了（表格里 `|` 多，被误判）——
    实测它会把 75% 的片段判掉，包括大量有真信息的表格。
    """
    if not text or len(text) < 30:
        return True
    n = len(text)

    # ① URL 编码串（%2F %3F %E4%B8 …）——这是最主要的垃圾来源
    pct = len(re.findall(r"%[0-9A-Fa-f]{2}", text))
    if pct >= 6 or pct / n > 0.012:
        return True

    # ② 一堆查询参数
    if len(re.findall(r"[?&][a-zA-Z_]{2,20}=", text)) >= 3:
        return True

    # ③ 剪藏常见图片链接尾巴
    if sum(text.count(k) for k in ("exptype=", "wx_fmt=", "tp=webp",
                                   "from=appmsg", "fasttmpl", "&amp;")) >= 3:
        return True

    # ④ 超长无空格串（base64 / data URI / 哈希）
    if re.search(r"[A-Za-z0-9+/=_-]{90,}", text):
        return True

    # ⑤ 剪藏头（--- 标题: … 原始链接: …）
    head = text.lstrip()[:200]
    if head.startswith("---") and ("原始链接" in text[:400] or "标题:" in head):
        return True

    # ⑥ 几乎全是外文/符号，没有中文正文
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk / n < 0.08:
        return True

    return False


def _kb_is_noise(text: str) -> bool:             # noqa: D401
    """本脚本用自己的 is_junk，不走全局那个激进版本。"""
    return is_junk(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的删除（不加只演练）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 找出标了「知识库」的文件夹
    kb_ids = []
    for r in conn.execute("SELECT id, path, config FROM folders"):
        try:
            cfg = json.loads(r["config"]) if r["config"] else {}
        except (ValueError, TypeError):
            cfg = {}
        if cfg.get("use_kb"):
            kb_ids.append((r["id"], r["path"]))
    if not kb_ids:
        print("  没有标为「知识库」的文件夹")
        return 1

    print("  知识库文件夹：")
    for i, p in kb_ids:
        print(f"    [{i}] {p}")
    print()

    placeholders = ",".join("?" * len(kb_ids))
    ids = [i for i, _ in kb_ids]
    rows = conn.execute(
        f"SELECT ch.id, ch.text, fi.name FROM chunks ch "
        f"JOIN files fi ON fi.id = ch.file_id "
        f"WHERE fi.folder_id IN ({placeholders}) AND ch.text <> ''", ids).fetchall()
    print(f"  这些文件夹的片段总数：{len(rows)}")

    noise, keep = [], 0
    for r in rows:
        if _kb_is_noise(r["text"] or ""):
            noise.append(r["id"])
        else:
            keep += 1

    print(f"    判为噪声 : {len(noise)}")
    print(f"    保留正文 : {keep}")
    if rows:
        print(f"    噪声占比 : {len(noise) / len(rows) * 100:.1f}%")
    print()

    # 抽样展示
    if noise:
        print("  噪声样例（前 3 条）：")
        for r in conn.execute(
                f"SELECT ch.text, fi.name FROM chunks ch JOIN files fi ON fi.id=ch.file_id "
                f"WHERE ch.id IN ({','.join('?' * min(3, len(noise)))})",
                noise[:3]):
            t = (r["text"] or "").replace("\n", " ")[:90]
            print(f"    · {r['name'][:30]}")
            print(f"      {t}")
        print()

    if not args.apply:
        print("  （演练模式，没有删任何东西。确认后加 --apply 执行）")
        if noise:
            print(f"  真要执行会删 {len(noise)} 个片段，然后你重新提问试试效果。")
        conn.close()
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = DB.with_name(f"index-backup-{stamp}.db")
    print(f"  备份索引库 → {bak.name} …")
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(bak)
    src.backup(dst)
    dst.close()
    src.close()
    print(f"  备份完成（{bak.stat().st_size / 1e6:.0f} MB）")

    with conn:
        for i in range(0, len(noise), 500):
            batch = noise[i:i + 500]
            conn.execute(
                f"DELETE FROM chunks WHERE id IN ({','.join('?' * len(batch))})", batch)
        # 同步每个文件的片段计数
        conn.execute(
            "UPDATE files SET n_chunks = "
            "(SELECT COUNT(*) FROM chunks WHERE file_id = files.id) "
            f"WHERE folder_id IN ({placeholders})", ids)

    left = conn.execute(
        f"SELECT COUNT(*) c FROM chunks ch JOIN files fi ON fi.id=ch.file_id "
        f"WHERE fi.folder_id IN ({placeholders})", ids).fetchone()["c"]
    print(f"  ✓ 已删除 {len(noise)} 个噪声片段，剩余 {left} 个")
    print()
    print("  现在回界面重新提问试试 —— 检索应该能命中真正的正文了。")
    print(f"  想还原：把 {bak.name} 拷回 index.db（先停服务）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
