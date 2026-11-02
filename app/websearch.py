"""联网搜索：抓 Bing 的 HTML 结果，不依赖任何 API key。

为什么用 Bing 而不是 DuckDuckGo：实测这台机器上 DDG 的 TLS 握手直接超时
（`_ssl.c:993: The handshake operation timed out`），Bing 正常返回。

为什么抓 HTML 而不是买 API：
  · 项目主要的是"能查一下网上的资料"，不是高并发生产级搜索
  · 免费额度、key 管理都是额外负担
  · 实测 Bing 的 `li.b_algo` 结构稳定，标题/链接/摘要都能干净取出来 ✓

风险与对策：
  · 抓取会被限流/偶尔出验证码 → 失败时**静默降级**（返回空，聊天照常回答），
    并在结果里告诉用户"这次没搜到网"，不阻断对话 ✓
  · 抓到的正文必须过 `clean_text`（AGENTS.md 第 7 条），否则脏字符会进提示词
"""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request

log = logging.getLogger("eg2.websearch")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 15
MAX_RESULTS = 8


def _clean(text: str) -> str:
    """去标签 + 解实体 + 压空白。搜索结果里会夹 `<strong>` 高亮标签。"""
    s = re.sub(r"<[^>]+>", "", text or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _searxng(base_url: str, query: str, n: int) -> list[dict]:
    """用自建/公共 SearXNG 搜（返回 JSON，比抓 HTML 稳）。

    项目主可以自己在 config.ini 里配：
        [search]
        web_provider = searxng
        searxng_url  = http://192.168.1.10:8888
    """
    base = base_url.rstrip("/")
    url = (base + "/search?q=" + urllib.parse.quote(query)
           + "&format=json&language=zh-CN&safesearch=0")
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    out = []
    for x in (data.get("results") or [])[:n]:
        out.append({"title": _clean(str(x.get("title") or ""))[:120],
                    "url": str(x.get("url") or "")[:300],
                    "snippet": _clean(str(x.get("content") or ""))[:400]})
    return [x for x in out if x["title"] or x["snippet"]]


def search(query: str, count: int = 5, provider: str = "bing",
           searxng_url: str = "") -> dict:
    """搜一下。**永不抛异常**——搜索失败不该让聊天也失败。

    返回 `{"ok": bool, "query": str, "results": [...], "error": str}`
    每条结果：`{"title", "url", "snippet"}`
    """
    q = (query or "").strip()
    out: dict = {"ok": False, "query": q, "results": [], "error": ""}
    if not q:
        out["error"] = "搜索词是空的"
        return out

    n = max(1, min(int(count or 5), MAX_RESULTS))
    # ★ 2026-10-22：搜索源可选（bing 抓 HTML / searxng 走 JSON）
    if (provider or "").lower() == "searxng" and searxng_url:
        try:
            rs = _searxng(searxng_url, q, n)
            out["ok"] = True
            out["results"] = rs
            out["provider"] = "searxng"
            if not rs:
                out["error"] = "没有搜到结果"
            log.info("SearXNG 搜索「%s」→ %d 条", q, len(rs))
            return out
        except Exception as exc:                                 # noqa: BLE001
            out["error"] = f"SearXNG 失败，改用 Bing：{exc}"
            log.warning("SearXNG 失败（%s）：%s", q, exc)
    out["provider"] = "bing"
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote(q)
           + "&count=" + str(n) + "&setlang=zh-CN")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept-Language": "zh-CN,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:                                     # noqa: BLE001
        out["error"] = f"搜索请求失败：{exc}"
        log.warning("联网搜索失败（%s）：%s", q, exc)
        return out

    blocks = re.findall(r'<li class="b_algo".*?</li>', body, re.S)
    if not blocks:
        # 没结果不算错误（可能是极冷门词），但要说清楚
        out["ok"] = True
        out["error"] = "没有搜到结果"
        return out

    results = []
    for b in blocks[:n]:
        m_title = re.search(r"<h2[^>]*>(.*?)</h2>", b, re.S)
        m_link = re.search(r'<a[^>]+href="(https?://[^"]+)"', b)
        m_cap = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
        title = _clean(m_title.group(1)) if m_title else ""
        link = html.unescape(m_link.group(1)) if m_link else ""
        snippet = _clean(m_cap.group(1)) if m_cap else ""
        if not title and not snippet:
            continue
        results.append({"title": title[:120], "url": link[:300], "snippet": snippet[:400]})
    out["ok"] = True
    out["results"] = results
    if not results:
        out["error"] = "搜到了页面但没解析出结果"
    log.info("联网搜索「%s」→ %d 条", q, len(results))
    return out


def as_context(payload: dict) -> str:
    """把搜索结果拼成给模型看的上下文。

    明确告诉模型：这些是**实时抓的网页摘要**，可能过时或不准确，
    要标注来源编号，不要当成自己的记忆。
    """
    items = (payload or {}).get("results") or []
    if not items:
        return ""
    lines = ["以下是刚刚从网上搜到的资料（实时抓取，可能有噪声或过时，"
             "引用时请标 [W1] [W2] 这样的编号）："]
    for i, it in enumerate(items, 1):
        lines.append(f"[W{i}] {it.get('title', '')}\n"
                     f"      {it.get('snippet', '')}\n"
                     f"      来源：{it.get('url', '')}")
    return "\n".join(lines)
