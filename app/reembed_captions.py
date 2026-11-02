# -*- coding: utf-8 -*-
"""修 caption 向量用的 prompt（漏了 Document），并重新编码已有的描述。

背景：indexer 给文字片段算向量时用了 prompt_name="Document"，
      而 caption._save 里是 engine.encode([caption]) —— 没有 prompt。
      两者不在同一向量空间，导致打标过的图在知识库里排不上来。
      （实测：查「河北大学」，正确的那张 caption 只有 0.68，
        而无关的文字片段能到 0.75。）

修法：
  1) caption.py 的 _save 改用 Document prompt（对新打标的生效）
  2) 本脚本把已有的 caption 重新编码一遍（只算向量，不调 VL 模型）
"""
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
import pathlib
import sqlite3
import sys

import numpy as np

sys.path.insert(0, r"D:\lingxi")

# ---------------- 1) 修 caption.py ----------------
c = pathlib.Path(r"D:\lingxi\app\caption.py")
t = c.read_text(encoding="utf-8")
old = "            v = engine.encode([caption])[0]"
new = ("            # 必须和文字片段用同一个 prompt（indexer 里是 \"Document\"），\n"
       "            # 否则两者不在同一向量空间，知识库检索根本排不上来。\n"
       "            _dim = getattr(getattr(store, \"cfg\", None), \"index_dim\", None)\n"
       "            v = engine.encode([caption], prompt_name=\"Document\",\n"
       "                              dim=_dim)[0]")
assert old in t, "caption._save 的 encode 没匹配到"
t = t.replace(old, new, 1)
c.write_text(t, encoding="utf-8")
print("  1) caption.py 已改用 Document prompt")

# ---------------- 2) 重新编码已有 caption ----------------
from app.config import Config            # noqa: E402
from app.model import EmbeddingEngine     # noqa: E402

cfg = Config()
eng = EmbeddingEngine(cfg)
eng.load()
print("  2) 模型已加载 device=%s dim=%s" % (eng.device, cfg.index_dim))

db = sqlite3.connect(cfg.db_path)
db.row_factory = sqlite3.Row
rows = db.execute(
    "SELECT id, text, LENGTH(vec) AS blen FROM chunks WHERE kind='caption'"
).fetchall()
print("  待重编码 caption: %d 条" % len(rows))

BATCH = 64
done = 0
for i in range(0, len(rows), BATCH):
    part = rows[i:i + BATCH]
    texts = [(r["text"] or "").strip() or "（空描述）" for r in part]
    vecs = eng.encode(texts, prompt_name="Document", dim=cfg.index_dim)
    for r, v in zip(part, vecs):
        a = np.asarray(v, dtype=np.float32)
        db.execute("UPDATE chunks SET vec=?, dim=? WHERE id=?",
                   (a.tobytes(), int(a.shape[0]), r["id"]))
    db.commit()
    done += len(part)
    if done % 640 == 0 or done >= len(rows):
        print("    已重编码 %d/%d" % (done, len(rows)))

print("  ✓ 完成，共 %d 条" % done)
print()
print("  抽查那条闫娇的:")
r = db.execute("SELECT id, dim, LENGTH(vec) AS n FROM chunks "
               "WHERE kind='caption' AND text LIKE '%闫娇%'").fetchone()
print("    chunk %s  dim=%s  向量字节=%s" % (r["id"], r["dim"], r["n"]))
