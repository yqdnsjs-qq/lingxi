"""调 Dify 的知识库检索（我们 → Dify）。

和已有的 `/dify/` 接口**方向相反**：
  · `/dify/retrieval`：Dify 把我们当外部知识库（Dify → 我们）
  · 本模块：聊天时去 Dify 那边检索（我们 → Dify）✓

Dify 的接口（Dataset Retrieval API）：
    POST {base_url}/v1/datasets/{dataset_id}/retrieve
    Authorization: Bearer {dataset_api_key}
    {"query": "...", "retrieval_model": {"search_method": "semantic_search",
                                          "top_k": 3, "score_threshold_enabled": false}}

配置（config.ini）：
    [dify]
    base_url    = http://192.168.1.10:5001     ← 你的 Dify 地址（末尾不要带 /v1）
    dataset_id  = xxxxxxxx-xxxx-xxxx           ← 知识库 ID
    dataset_key = dataset-xxxx                 ← 知识库 API 密钥
    （原来的 api_key 不变：那是**别人调我们**时用的）
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

log = logging.getLogger("eg2.difyclient")

TIMEOUT = 20

# Dify 的 dataset 必须是标准 UUID，否则路由根本不匹配（回 HTML 404）
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _norm_base(url: str) -> str:
    """把用户填的 Dify 地址收拾干净。

    ⚠ 2026-10-22 项目主就栽在这：Dify 知识库页面给的「API Base URL」是
    `http://192.168.1.30:8080/v1`，照抄进来，而 `retrieve()` 自己还要再拼
    `/v1/datasets/...` → 变成 `/v1/v1/datasets/...` → **404**（HTML 页面，
    看起来像"地址不存在"，其实只是多写了一段）✗
    所以这里统一：去掉末尾的 `/` 和重复的 `/v1`，用户怎么抄都能用 ✓
    """
    u = str(url or "").strip().rstrip("/")
    while u.lower().endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u


def _cfg(cfg) -> dict:                                           # noqa: ANN001
    return {
        "base_url": _norm_base(cfg._get("dify", "base_url", "")),
        "dataset_id": str(cfg._get("dify", "dataset_id", "") or "").strip(),
        "dataset_key": str(cfg._get("dify", "dataset_key", "") or "").strip(),
    }


def configured(cfg) -> bool:                                     # noqa: ANN001
    c = _cfg(cfg)
    return bool(c["base_url"] and c["dataset_id"] and c["dataset_key"])


def retrieve(cfg, query: str, top_k: int = 4) -> dict:           # noqa: ANN001
    """去 Dify 那边检索。**永不抛异常**（检索失败不该让聊天挂掉）。

    返回 `{"ok": bool, "results": [{"title", "snippet", "score"}], "error": str}`
    """
    q = (query or "").strip()
    out: dict = {"ok": False, "results": [], "error": ""}
    if not q:
        out["error"] = "查询是空的"
        return out
    c = _cfg(cfg)
    if not (c["base_url"] and c["dataset_id"] and c["dataset_key"]):
        out["error"] = "没配 Dify 知识库（config.ini 的 [dify] base_url/dataset_id/dataset_key）"
        return out
    # ★ 2026-10-22：Dify 的路由是 /v1/datasets/<uuid>/retrieve，
    #   dataset_id 不是完整 UUID 时 Flask 直接回一个 HTML 404（看着像"地址错"），
    #   极难自查。这里先挡一道，直接说清楚"少了多少位"✓
    if not _UUID_RE.match(c["dataset_id"]):
        out["error"] = (f"知识库 ID 不完整：Dify 要 36 位 UUID（8-4-4-4-12），"
                        f"现在填的是 {len(c['dataset_id'])} 位「{c['dataset_id']}」。"
                        "去 Dify 知识库 → 左侧「API」，重新复制完整的 ID。")
        log.warning("Dify 检索跳过：dataset_id 不是 UUID（%s）", c["dataset_id"])
        return out

    url = f"{c['base_url']}/v1/datasets/{c['dataset_id']}/retrieve"
    body = {"query": q,
            "retrieval_model": {"search_method": "semantic_search",
                                "reranking_enable": False,
                                "top_k": max(1, min(int(top_k), 10)),
                                "score_threshold_enabled": False}}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + c["dataset_key"],
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:                                        # noqa: BLE001
            pass
        if detail.lstrip().startswith("<"):
            # Dify（Flask）路由没匹配上就是一张 HTML 404 页面 —— 不是我们的报文，
            # 直接翻译成人话，别把半页 HTML 塞给用户 ✗
            out["error"] = (f"Dify 说没有这个地址（HTTP {exc.code}）。"
                            "常见原因：① 地址多写了 /v1（只填到端口就行）；"
                            "② 知识库 ID 复制不完整；③ 这个库不存在或没授权。")
        else:
            out["error"] = f"Dify 返回 HTTP {exc.code}：{detail}"
        log.warning("Dify 检索失败：%s", out["error"])
        return out
    except Exception as exc:                                     # noqa: BLE001
        out["error"] = f"连不上 Dify：{exc}"
        log.warning("Dify 检索失败：%s", exc)
        return out

    results = []
    for rec in (data.get("records") or [])[:top_k]:
        seg = rec.get("segment") or {}
        doc = (seg.get("document") or {}).get("name") or ""
        txt = str(seg.get("content") or "").strip()
        if not txt:
            continue
        results.append({"title": doc or "Dify 片段",
                        "snippet": txt[:900],
                        "score": round(float(rec.get("score") or 0), 3)})
    out["ok"] = True
    out["results"] = results
    if not results:
        out["error"] = "Dify 没检索到内容"
    log.info("Dify 检索「%s」→ %d 条", q, len(results))
    return out


def as_context(payload: dict) -> str:
    """拼成给模型看的上下文。"""
    items = (payload or {}).get("results") or []
    if not items:
        return ""
    lines = ["以下是用户另一个知识库（Dify）里检索到的片段"
             "（引用时标 [D1] [D2]）："]
    for i, it in enumerate(items, 1):
        lines.append(f"[D{i}] {it.get('title', '')}\n      {it.get('snippet', '')}")
    return "\n".join(lines)


def retrieve_multi(items: list[dict], query: str, top_k_each: int = 3) -> dict:
    """遍历用户的多个 Dify 知识库，合并结果。

    `items` 每项：`{name, base_url, dataset_id, dataset_key, enabled}`
    返回 `{"ok", "results": [...], "errors": [...], "used": [库名...]}`
    """
    q = (query or "").strip()
    out: dict = {"ok": False, "results": [], "errors": [], "used": []}
    if not q:
        out["errors"].append("查询是空的")
        return out
    live = [x for x in (items or []) if x.get("enabled", True)
            and x.get("base_url") and x.get("dataset_id") and x.get("dataset_key")]
    if not live:
        out["errors"].append("没有可用的 Dify 知识库（去设置里加一个）")
        return out

    for it in live[:8]:                     # 最多同时查 8 个库
        cfg_like = _FakeCfg(it)
        one = retrieve(cfg_like, q, top_k_each)
        name = str(it.get("name") or it.get("dataset_id") or "Dify")
        if one.get("ok") and one.get("results"):
            out["used"].append(name)
            for r in one["results"]:
                out["results"].append({**r, "source": name})
        elif one.get("error"):
            out["errors"].append(f"{name}：{one['error']}")
    out["ok"] = bool(out["results"])
    if not out["ok"] and not out["errors"]:
        out["errors"].append("所有 Dify 库都没检索到内容")
    log.info("Dify 多库检索「%s」→ %d 条（%s）", q, len(out["results"]),
             "、".join(out["used"]) or "无")
    return out


class _FakeCfg:
    """把一条 dify 配置伪装成 cfg，好复用 retrieve()。"""

    def __init__(self, item: dict) -> None:
        self._item = item

    def _get(self, section: str, key: str, default: str = "") -> str:   # noqa: ANN001
        if section != "dify":
            return default
        m = {"base_url": "base_url", "dataset_id": "dataset_id",
             "dataset_key": "dataset_key"}
        return str(self._item.get(m.get(key, key), default) or default)


def as_context_multi(payload: dict) -> str:
    """多库结果拼上下文（带上库名，便于回答里说清是哪个库）。"""
    items = (payload or {}).get("results") or []
    if not items:
        return ""
    lines = ["以下是用户自己接的 Dify 知识库里检索到的片段"
             "（引用时标 [D1] [D2]）："]
    for i, it in enumerate(items, 1):
        src = it.get("source") or ""
        lines.append(f"[D{i}] {it.get('title', '')}"
                     + (f"（来自「{src}」）" if src else "")
                     + f"\n      {it.get('snippet', '')}")
    return "\n".join(lines)
