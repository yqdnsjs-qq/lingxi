"""外部模型后端：Ollama / OpenAI 兼容接口。

用在哪
------
打标时可以不用本机的 VL 模型，改用局域网里另一台机器上的大模型：
  · 本机 8 GB 卡继续做搜索、人脸
  · 另一台的大卡专门看图打标
互不抢显存，还能并行。

支持的接口
----------
1. Ollama        POST {host}/api/generate   （原生支持 images: [base64]）
2. OpenAI 兼容   POST {host}/v1/chat/completions（image_url + data URI）

注意
----
**必须是视觉模型**。纯文本模型（如 Qwen3-27B）看不到图，
会对每张图编出同一句话 —— 这个坑在本机 VL 上已经踩过一次。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import socket
import time
import urllib.error
import urllib.request

log = logging.getLogger("eg2.vlm")

# ---- 网络：绕开系统代理 + 优先 IPv4 ----
# 为什么要这么做（实测数据）：
#   · 系统代理 127.0.0.1:10808 开着时，urllib 会读注册表自动走代理，
#     到 opencode.ai 的握手要 41 秒甚至超时；直连只要 21 秒。
#   · Cloudflare 常返回 AAAA（IPv6）记录，IPv6 路由一差就握手超时。
# 所以这里统一：不走代理 + 优先 IPv4。两者都只在**外部请求**里生效，
# 不影响本机服务（127.0.0.1 本来就在 ProxyOverride 里）。
_ORIG_GAI = socket.getaddrinfo


def _gai_ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    try:
        infos = _ORIG_GAI(host, port, socket.AF_INET, type, proto, flags)
        if infos:
            return infos
    except socket.gaierror:
        pass
    return _ORIG_GAI(host, port, family, type, proto, flags)


def _opener() -> urllib.request.OpenerDirector:
    """一个不走任何代理的 opener。

    ⚠ 2026-10-18 项目主问"调外部模型怎么这么慢、是不是走代理了"：
      `urllib.urlopen()` 默认会用 `getproxies()` —— 在 Windows 上它读**注册表里的
      系统代理**（WinINET 设置）✗，有时候还带 PAC 脚本，于是每个请求先绕一圈代理
      → 慢、甚至卡住。这里显式给**空代理**，等于强制直连 ✓
      （内网地址同理受益：本地/局域网本来就不该绕代理 ✓）
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout: int):
    """发请求：不走代理 + 优先 IPv4 + **记录真实耗时**（便于定位"慢"在哪）。"""
    t0 = time.monotonic()
    old = socket.getaddrinfo
    socket.getaddrinfo = _gai_ipv4_first
    try:
        resp = _opener().open(req, timeout=timeout)
        cost = time.monotonic() - t0
        if cost >= 3.0:                     # 只记慢的，别把日志刷满
            log.info("外部请求较慢：%.1fs  %s", cost, _short_url(req))
        else:
            log.debug("外部请求 %.1fs  %s", cost, _short_url(req))
        return resp
    except Exception as exc:                                     # noqa: BLE001
        log.warning("外部请求失败：%.1fs  %s  → %s",
                    time.monotonic() - t0, _short_url(req), exc)
        raise
    finally:
        socket.getaddrinfo = old


def _short_url(req) -> str:
    try:
        return str(req.full_url)[:110]
    except Exception:                                            # noqa: BLE001
        return "?"


PROMPT = ("用一句中文描述这张图片的内容，20-60 字。"
          "如果图里有文字，把关键文字也写出来。"
          "只输出描述本身，不要任何前缀或解释。")


def _b64_of(pil_image, max_side: int = 768) -> str:
    """把 PIL 图压成 JPEG 的 base64（不带 data URI 前缀）。"""
    im = pil_image
    if max(im.size) > max_side:
        r = max_side / max(im.size)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# 很多云服务挂了 Cloudflare，会直接 403 掉 Python-urllib 的默认 UA。
# 带上常见的浏览器风格头，能过绝大多数 WAF。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _headers(api_key: str | None, json_body: bool = True,
             extra: dict | None = None) -> dict:
    # ⚠ 2026-10-23：`extra` 传进来的可能是**JSON 字符串**（偏好里存的就是字符串），
    #   直接 `.items()` 会抛 "'str' object has no attribute 'items'"，报错长这样：
    #   `外部模型调用失败：'str' object has no attribute 'items'` ✗
    #   这里统一兜住：字符串先按 JSON 解，解不出来就当没有 ✓
    if isinstance(extra, str):
        s = extra.strip()
        try:
            extra = json.loads(s) if s else None
        except Exception:                                        # noqa: BLE001
            extra = None
    if not isinstance(extra, dict):
        extra = None
    h = {
        "User-Agent": _UA,
        "Accept": "application/json, text/event-stream, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if json_body:
        h["Content-Type"] = "application/json"
    if api_key:
        tok = api_key.strip()
        h["Authorization"] = tok if tok.lower().startswith("bearer ") else "Bearer " + tok
    # 自定义头放最后，允许覆盖上面的默认值
    for k, val in (extra or {}).items():
        if k and val is not None:
            h[str(k)] = str(val)
    return h


def _read_error(exc) -> str:
    """从 HTTPError 里把响应体读出来 —— 只报 403 看不到原因太难查。"""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:                                         # noqa: BLE001
        body = ""
    code = getattr(exc, "code", "?")
    hint = ""
    if code == 403:
        hint = ("  （403 通常是网关/WAF 拦了请求头，或者这个地址不支持 /models。"
                "可以直接在「模型名」里手填模型名，跳过探测。）")
    elif code == 401:
        hint = "  （401 = 密钥不对或没带上）"
    elif code == 404:
        hint = "  （404 = 地址路径不对，检查结尾是不是 /v1）"
    return f"HTTP {code}: {body[:200]}{hint}"


def caption_one(base_url: str, model: str, image, timeout: int = 180,
                max_side: int = 768, style: str = "auto",
                max_tokens: int = 200, prompt: str | None = None,
                api_key: str | None = None, extra_headers: dict | None = None,
                extra_body: dict | None = None) -> str:
    """调外部模型给一张图生成描述。

    base_url 形如 http://192.168.1.50:11434（Ollama）
              或 http://192.168.1.50:8080/v1（OpenAI 兼容）

    extra_body：往**请求体**里额外塞字段（不是请求头 ✓），比如
      `{"reasoning_effort": "none"}` —— 2026-10-20 实测：deepseek-v4.1-flash
      会把整段思考草稿写进 content（2086 字，还没写完就撞 max_tokens），
      加上这个字段才直接给答案 ✓（`enable_thinking=false` 无效，web 上不用试 ✗）
      只对 OpenAI 兼容分支生效；Ollama 分支忽略 ✓
    """
    b64 = _b64_of(image, max_side)
    ask = prompt or PROMPT
    url = base_url.rstrip("/")

    # OpenAI 兼容接口：路径里带 /v1
    if url.endswith("/v1") or "/v1/" in url:
        endpoint = url.rstrip("/") + "/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": ask},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]}],
            "max_tokens": max(32, min(int(max_tokens), 1024)), "temperature": 0.2,
        }
        for k, v in (extra_body or {}).items():       # ★ 额外请求体字段
            if k:
                body[k] = v
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=data, method="POST",
            headers=_headers(api_key, True, extra_headers))
        try:
            with _open(req, timeout=timeout) as resp:
                out = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_read_error(exc)) from exc

        ch = (out.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        # 推理模型（glm-5.x / deepseek-v4 / o 系列等）常把内容放在
        # reasoning_content，content 反而是空的。
        text = (msg.get("content") or "").strip()
        if not text:
            # 推理模型把内容放在 reasoning_content。它通常是「思考过程 +
            # 结论」，直接当描述会很啰嗦，所以尽量只取结论那一句。
            raw = (msg.get("reasoning_content") or "").strip()
            for mk in ("我会直接描述：", "最终答案：", "描述如下：",
                       "答案是：", "描述为：", "直接描述："):
                i = raw.rfind(mk)
                if i >= 0:
                    tail = raw[i + len(mk):].strip()
                    if len(tail) >= 8:
                        raw = tail
                        break
            text = raw
        if not text:
            text = (ch.get("text") or "").strip()
        if not text:
            # 真的空 —— 把原始结构带出来，好判断是超长思考还是被拦了
            keys = list(msg.keys())
            fin = ch.get("finish_reason")
            usage = out.get("usage") or {}
            raise RuntimeError(
                f"模型返回了空内容（finish_reason={fin}，message 字段={keys}，"
                f"用量={usage}）。"
                "常见原因：① 推理模型把 120 tokens 全用在思考上 —— "
                "把「最多 tokens」调到 800 再试；"
                "② 这个网关没真的把图片转过去。")
        return text

    # 默认按 Ollama 处理
    endpoint = url + "/api/generate"
    body = {"model": model, "prompt": ask, "images": [b64],
            "stream": False,
            "options": {"temperature": 0.2,
                        "num_predict": max(32, min(int(max_tokens), 1024))}}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data, method="POST",
        headers=_headers(api_key, True, extra_headers))
    try:
        with _open(req, timeout=timeout) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_read_error(exc)) from exc
    txt = str(out.get("response") or "").strip()
    if not txt:
        txt = str(out.get("thinking") or "").strip()
    if not txt:
        raise RuntimeError(
            f"模型返回了空内容（done_reason={out.get('done_reason')}）。"
            "把「最多 tokens」调大再试。")
    return txt


def probe(base_url: str, timeout: int = 90, api_key: str | None = None,
          extra_headers: dict | None = None) -> dict:
    """看看这个地址上有什么模型，并判断哪些可能能看图。"""
    url = base_url.rstrip("/")

    # 有些网关（尤其挂了 Cloudflare 的）建立连接就要几十秒，
    # 而且经常随机超时。所以给足超时，并且自动重试几次。
    last = None
    for attempt in range(3):
        try:
            return _probe_once(url, timeout, api_key, extra_headers)
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": _read_error(exc)}
        except Exception as exc:                              # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    return {"ok": False,
            "error": f"{type(last).__name__}: {last}"
                     "  （网络握手超时 —— 重试 3 次都没成功。"
                     "如果是 opencode.ai 这类境外地址，多半是线路不稳。）"}


def _probe_once(url: str, timeout: int, api_key: str | None,
                extra_headers: dict | None) -> dict:
    try:
        if url.endswith("/v1") or "/v1/" in url:
            try:
                req = urllib.request.Request(
                    url.rstrip("/") + "/models",
                    headers=_headers(api_key, False, extra_headers))
                with _open(req, timeout=timeout) as r:
                    d = json.loads(r.read())
                models = [x.get("id") for x in (d.get("data") or [])]
                return {"ok": True, "kind": "openai", "models": models}
            except urllib.error.HTTPError as exc:
                # /models 不通不代表不能用 —— 有些网关只放行 chat 路径。
                # 这时告诉用户手填模型名，别卡在探测这一步。
                return {"ok": False, "kind": "openai",
                        "error": _read_error(exc),
                        "can_manual": exc.code in (403, 404, 405)}
        req = urllib.request.Request(
            url + "/api/tags", headers=_headers(api_key, False, extra_headers))
        with _open(req, timeout=timeout) as r:          # ★ 也要不走代理 ✓（原来这里是 urlopen ✗）
            d = json.loads(r.read())
        models = []
        for m in d.get("models") or []:
            fam = str((m.get("details") or {}).get("family") or "")
            models.append({"name": m.get("name"), "family": fam,
                           "gb": round((m.get("size") or 0) / 1e9, 1)})
        return {"ok": True, "kind": "ollama", "models": models}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": _read_error(exc)}


def chat_stream(base_url: str, model: str, messages: list, timeout: int = 180,
                max_tokens: int = 1200, temperature: float = 0.3,
                api_key: str | None = None,
                extra_headers: dict | None = None):
    """流式对话：逐段 yield 文本（解析 SSE 的 data: 行）。

    项目主 2026-10-22 要的"流式输出"：长回答不用干等整段返回。
    和 chat_once 只差 `stream: true` + 逐行解析。
    """
    url = base_url.rstrip("/")
    if not (url.endswith("/v1") or "/v1/" in url):
        raise RuntimeError("外部问答需要 OpenAI 兼容地址（结尾带 /v1）")
    body = {"model": model, "messages": messages, "stream": True,
            "max_tokens": max(64, min(int(max_tokens), 4096)),
            "temperature": max(0.0, min(float(temperature), 1.5))}
    req = urllib.request.Request(
        url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
        method="POST", headers=_headers(api_key, True, extra_headers))
    try:
        with _open(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except Exception:                                # noqa: BLE001
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield delta
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_read_error(exc)) from exc


def chat_once(base_url: str, model: str, messages: list, timeout: int = 600,
              max_tokens: int = 900, temperature: float = 0.2,
              api_key: str | None = None,
              extra_headers: dict | None = None) -> str:
    """通用对话（纯文本）——给知识库问答用外部模型。

    和 caption_one 的区别：这个接受完整的 messages 数组，
    所以能把检索到的资料片段 + 问题一起发过去。
    """
    url = base_url.rstrip("/")
    if not (url.endswith("/v1") or "/v1/" in url):
        raise RuntimeError("外部问答需要 OpenAI 兼容地址（结尾带 /v1）")
    endpoint = url + "/chat/completions"
    body = {"model": model, "messages": messages,
            "max_tokens": max(64, min(int(max_tokens), 4096)),
            "temperature": max(0.0, min(float(temperature), 1.5))}
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode("utf-8"), method="POST",
        headers=_headers(api_key, True, extra_headers))
    try:
        with _open(req, timeout=timeout) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_read_error(exc)) from exc
    ch = (out.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        raw = (msg.get("reasoning_content") or "").strip()
        for mk in ("最终答案：", "答案：", "回答：", "总结："):
            i = raw.rfind(mk)
            if i >= 0 and len(raw[i + len(mk):].strip()) >= 8:
                raw = raw[i + len(mk):].strip()
                break
        text = raw
    if not text:
        raise RuntimeError(f"外部模型返回空内容（finish_reason={ch.get('finish_reason')}）")
    return text
