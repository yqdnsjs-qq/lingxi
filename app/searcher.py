"""检索：把查询语句编码成向量，再和索引里的向量比对。

除了纯向量相似度，还会对文件名/路径做包含匹配加权，
所以「输入法」「2024 报销」这种带关键词的查询也能精准命中。

多人 ⑤ 起多了一路：**FTS5 关键词通道**（片段正文的子串匹配，见 app/ftsindex.py）。
三路（向量 / 文件名 / FTS）用 RRF 融合 ✓ —— 因为分数量纲完全不同，直接加权不稳。
"""
from __future__ import annotations

import logging
from typing import Any

from .config import Config
from .model import EmbeddingEngine
from .store import Store

log = logging.getLogger("eg2.search")

# RRF 的 K 和权重：与 store.hybrid_search 里那套保持一致的口径 ✓
RRF_K = 60.0
CHANNEL_W = {"semantic": 1.0, "literal": 0.8, "fts": 1.1}


def rrf_fuse(rankings: dict[str, list[int]],
             weights: dict[str, float] | None = None) -> dict[int, float]:
    """倒数排名融合：每个通道给一份**有序的 id 列表**，返回 {id: 融合分}。

    RRF 的好处是**不需要**把余弦相似度和 bm25 分数对齐量纲 ✓
    （它们本来就是两种完全不同的刻度，硬加权很容易被某一路压过去 ✗）。
    """
    w = dict(CHANNEL_W)
    w.update(weights or {})
    fused: dict[int, float] = {}
    for channel, ids in rankings.items():
        weight = w.get(channel, 0.5)
        for rank, fid in enumerate(ids):
            fused[int(fid)] = fused.get(int(fid), 0.0) + weight / (RRF_K + rank + 1)
    return fused


class Searcher:
    def __init__(self, cfg: Config, store: Store, engine: EmbeddingEngine):
        self.cfg = cfg
        self.store = store
        self.engine = engine

    def search(self, query: str, top_k: int = 30,
               folder_ids: list[int] | None = None,
               exts: list[str] | None = None) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"query": query, "count": 0, "results": [], "elapsed": 0.0}

        import time

        started = time.perf_counter()
        # 查询用模型完整维度编码，再按各文件夹的索引维度切片比对，
        # 这样「A 文件夹 128 维、B 文件夹 512 维」也能一起搜。
        vector = self.engine.encode([query], prompt_name="SearchQuery")[0]
        results = self.store.search(vector, top_k=max(top_k, 30), folder_ids=folder_ids,
                                    exts=exts or None, literal=query)

        # ---- 关键词通道（FTS5）：子串级精确命中，专有名词靠它 ✓ ----
        fts_hits = self.store.fts_search(query, top_k=max(top_k * 2, 60),
                                         folder_ids=folder_ids)
        if exts:
            fts_hits = [h for h in fts_hits if (h["ext"] or "") in exts]

        # 文件名/路径包含匹配兜底（查询里没有空格时按整串匹配）
        # ★ 2026-11-03：这一步是**整表 LIKE 扫描**，62 万行实测 10.4 秒；
        #   而 FTS 通道干的是同一件事、只要 0.01 秒。
        #   所以：FTS 已经命中就不再扫全表（扫一次就是 10 秒，用户会以为卡死）✗
        lit_hits: list[dict] = []
        if " " not in query and len(query) >= 2:
            for item in self.store.literal_search(query, top_k=top_k):
                if folder_ids and item.get("folder_id") not in folder_ids:
                    continue
                if exts and (item["ext"] or "") not in exts:
                    continue
                lit_hits.append(item)

        if fts_hits:
            # 三路 RRF：把向量结果、FTS 结果、文件名结果按**排名**融合 ✓
            by_id = {int(r["id"]): r for r in results}
            for item in fts_hits + lit_hits:
                by_id.setdefault(int(item["id"]), item)
            rankings = {
                "semantic": [int(r["id"]) for r in results],
                "fts": [int(r["id"]) for r in fts_hits],
                "literal": [int(r["id"]) for r in lit_hits],
            }
            fused = rrf_fuse({k: v for k, v in rankings.items() if v})
            top = max(fused.values()) if fused else 1.0
            merged: list[dict] = []
            for fid, score in sorted(fused.items(), key=lambda kv: -kv[1]):
                item = dict(by_id.get(fid) or {})
                if not item:
                    continue
                item["score"] = round(score / (top or 1.0) * 100, 2)   # 归一化成 0~100，界面照旧显示 ✓
                item["channels"] = {
                    ch: {"rank": ids.index(fid)}
                    for ch, ids in rankings.items() if fid in ids}
                if fid in {int(h["id"]) for h in fts_hits}:
                    hit = next(h for h in fts_hits if int(h["id"]) == fid)
                    if hit.get("snippet"):
                        item["snippet"] = hit["snippet"]
                merged.append(item)
            results = merged[:top_k]
            log.debug("RRF 融合：向量 %d + FTS %d + 文件名 %d → %d 条",
                      len(rankings["semantic"]), len(rankings["fts"]),
                      len(rankings["literal"]), len(results))
        else:
            # 没有关键词命中 → 保持原来的向量+文件名打分（老行为不变 ✓）
            if lit_hits:
                seen = {item["path"] for item in results}
                for item in lit_hits:
                    if item["path"] in seen:
                        continue
                    name = item["name"].lower()
                    item["score"] = round(0.45 + (0.25 if query.lower() in name else 0.0), 4)
                    results.append(item)
            results.sort(key=lambda row: -row["score"])
            results = results[:top_k]

        return {
            "query": query,
            "count": len(results),
            "results": results,
            "elapsed": round(time.perf_counter() - started, 3),
            "fts": len(fts_hits),
        }

    def hybrid(self, query: str = "", *, top_k: int = 30,
               folder_ids: list[int] | None = None,
               exts: list[str] | None = None,
               person_ids: list[int] | None = None,
               person_mode: str = "filter",
               face_vec=None) -> dict[str, Any]:
        """混合搜索：文件名 + 语义 + 人物 + 人脸，多路召回后融合排序。

        可以只给人物（搜「张三的所有照片」），也可以只给一张脸
        （以图搜人），还能「人物 + 关键词」一起用（「张三 海边」）。
        """
        import time

        started = time.perf_counter()
        text = (query or "").strip()
        vector = None
        if text:
            vector = self.engine.encode([text], prompt_name="SearchQuery")[0]
        results = self.store.hybrid_search(
            vector, top_k=top_k, folder_ids=folder_ids, exts=exts or None,
            literal=text, person_ids=person_ids, person_mode=person_mode,
            face_vec=face_vec,
            face_threshold=self.cfg.face_search_threshold)
        return {
            "query": text,
            "count": len(results),
            "results": results,
            "elapsed": round(time.perf_counter() - started, 3),
            "person_mode": person_mode,
        }

    def name_preview(self, path: str) -> str:
        return path
