"""聊天标签页的接口：会话保存 + 发消息 + 联网搜索。

和已有的 `/api/chat`（知识库问答）**分开**：
  · `/api/chat`        —— 必须先检索知识库，答不上来就报"没有知识库"
  · `/api/chat2/...`   —— 纯聊天，可选带知识库 / 可选联网，会话能保存

多人隔离：会话按 `user_id` 归属，**只能看自己的**（管理员也一样，别偷看别人聊天 ✗）。
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import (APIRouter, Body, File, HTTPException, Query, Request,
                   UploadFile)
from pathlib import Path

import base64
import mimetypes
import os
import uuid

from . import difyclient, websearch
from .config import Config
from .store import Store

log = logging.getLogger("eg2.chat2")

MAX_TITLE = 40

# 名字里带这些的，基本能看图（宁可多提醒，也别让人白传）
_VL_HINTS = ("vl", "vision", "gemini", "gpt-4o", "gpt-4.1", "gpt-5",
             "claude-3", "claude-4", "claude-sonnet", "llava", "pixtral",
             "qwen-vl", "qwen2-vl", "qwen2.5-vl", "glm-4v", "internvl",
             "minicpm-v", "phi-3-vision", "llama-3.2-vision")


def looks_vl(model: str) -> bool:
    """按模型名猜能不能看图。猜不准就返回 False（让前端提醒一句）。"""
    m = str(model or "").lower()
    return any(h in m for h in _VL_HINTS)





def strip_think(text: str) -> str:
    """剥掉模型吐出来的思考过程（`<think>…</think>`）。

    有的模型会把 CoT 直接写在 content 里，界面上就变成
    "让我想想… / 用户发送了一条消息…" 这种无关内容 ✗
    """
    import re as _re

    s = str(text or "")
    s = _re.sub(r"<think[\s\S]*?</think\s*>", "", s, flags=_re.I)
    s = _re.sub(r"^\s*<think[\s\S]*$", "", s, flags=_re.I)      # 没闭合的
    return s.strip()


def create_router(cfg: Config, store: Store, engine=None) -> APIRouter:
    router = APIRouter()
    _searcher = None

    def searcher():                                              # noqa: ANN202
        nonlocal _searcher
        if _searcher is None and engine is not None:
            from .searcher import Searcher

            _searcher = Searcher(cfg, store, engine)
        return _searcher

    # ---------- 身份 ----------
    def _me(request) -> dict:                                     # noqa: ANN001
        """当前用户。不能确定身份时**拒绝**（聊天记录是私密的）。"""
        try:
            from .auth import COOKIE_NAME, Auth

            u = Auth(cfg.db_path).who(request.cookies.get(COOKIE_NAME))
        except Exception:                                        # noqa: BLE001
            u = None
        if not u:
            raise HTTPException(status_code=401, detail="请先登录再聊天（会话要按人保存）")
        return u

    # ★ 换库 / 重启时把 store 一起换掉（不注册的话重启后这里全是
    #   "Cannot operate on a closed database"，而且被 except 吞掉 → 静默返回空 ✗）
    from .dbreload import register as _register_reload

    def _rebind_db(new_store) -> None:
        nonlocal store, _searcher
        store = new_store
        _searcher = None

    _register_reload("chat2-router", _rebind_db)

    def _local() -> bool:
        """本机（建库机）= 自己的机器，配置不隔离。"""
        try:
            from .filesearch import _can_index_here

            return bool(_can_index_here())
        except Exception:                                        # noqa: BLE001
            return False

    def _admin_view(request) -> bool:                            # noqa: ANN001
        """管理员 或 本机 → 能用带密钥的配置。"""
        u = _me(request)
        return (str(u.get("role")) == "admin") or _local()

    def _prefs(request) -> dict:                                 # noqa: ANN001
        """当前用户的偏好。**普通用户不继承管理员的 API 凭据** ✓"""
        u = _me(request)
        is_admin = (str(u.get("role")) == "admin") or _local()
        return store.prefs_all(int(u.get("id") or 0), inherit_sensitive=is_admin)

    def _me_user(request) -> dict:                              # noqa: ANN001
        """给 multiuser 用的小字典（只要 id/role）。"""
        u = _me(request)
        return {"id": int(u.get("id") or 0), "role": str(u.get("role") or "")}

    def _uid(request) -> int:                                     # noqa: ANN001
        return int(_me(request).get("id") or 0)

    def _user_name(uid: int) -> str:                              # noqa: ANN001
        try:
            from .auth import Auth

            u = Auth(cfg.db_path).get(int(uid))
            return str((u or {}).get("name") or ("#%d" % int(uid)))
        except Exception:                                        # noqa: BLE001
            return "#%d" % int(uid)

    def _api_items_for(request) -> list[dict]:                    # noqa: ANN001
        """当前用户"能用"的外部 API 配置（规则见 app/apimodels.py）。

        2026-10-22 项目主报的 bug 就修在这里：普通用户登录服务器后连**没密钥的
        llama.cpp** 都看不到 —— 因为配置在管理员自己的 user_prefs 里。
        现在统一走 apimodels.items_for()：自己的全部 + 别人的公共/共享 ✓
        """
        from . import apimodels

        return apimodels.items_for(store, _uid(request),
                                   str(_me(request).get("name") or ""))

    # ---------- 外部模型配置（和设置页共用 eg2-rm*）----------
    def _remote(request) -> dict | None:                          # noqa: ANN001
        try:
            prefs = _prefs(request)
        except Exception:                                        # noqa: BLE001
            return None
        url = str(prefs.get("eg2-rmurl") or "").strip()
        model = str(prefs.get("eg2-rmmodel") or "").strip()
        if not url or not model:
            return None
        hdr = None
        raw = str(prefs.get("eg2-rmhdr") or "").strip()
        if raw:
            try:
                hdr = json.loads(raw)
            except Exception:                                    # noqa: BLE001
                hdr = None
        return {"base_url": url, "model": model,
                "api_key": str(prefs.get("eg2-rmkey") or ""),
                "headers": hdr, "max_tokens": 1200}

    # ---------- 附件上传 ----------
    UPLOAD_DIR = Path(cfg.data_dir) / "chat_uploads" if hasattr(cfg, "data_dir") \
        else Path(__file__).resolve().parent.parent / "data" / "chat_uploads"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    MAX_UPLOAD = 25 * 1024 * 1024        # 25 MB

    @router.post("/api/chat2/upload")
    async def upload(request: Request, file: UploadFile = File(...)):
        """收一个文件，存下来，返回 id + 类型（图片 / 文本）。"""
        from . import ratelimit as _rl

        _ok, _wait = _rl.hit("upload", _rl.client_ip(request))
        if not _ok:
            raise HTTPException(status_code=429,
                                detail=f"上传太频繁，请 {_wait} 秒后再试")
        uid = _uid(request)
        raw = await file.read()
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(status_code=413,
                                detail=f"文件太大（上限 {MAX_UPLOAD // 1024 // 1024} MB）")
        if not raw:
            raise HTTPException(status_code=400, detail="空文件")
        name = os.path.basename(file.filename or "file")
        ext = os.path.splitext(name)[1].lower()
        fid = uuid.uuid4().hex
        # 文件名里带上 uid，避免不同人互相引用
        dest = UPLOAD_DIR / f"u{uid}_{fid}{ext}"
        dest.write_bytes(raw)
        kind = "image" if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp") \
            else "file"
        return {"ok": True, "id": dest.name, "name": name, "ext": ext,
                "kind": kind, "size": len(raw)}

    def _load_attachments(request, ids) -> tuple[list, list]:     # noqa: ANN001
        """把手里的附件变成 (图片块列表, 文本上下文列表)。

        只认**自己传的**（文件名以 `u<uid>_` 开头）✓
        """
        uid = _uid(request)
        imgs: list[dict] = []
        texts: list[str] = []
        for raw_id in (ids or [])[:6]:                          # 最多 6 个
            aid = os.path.basename(str(raw_id))
            if not aid.startswith(f"u{uid}_"):
                continue                                        # 不是自己传的，忽略
            f = UPLOAD_DIR / aid
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
                mime = mimetypes.guess_type(aid)[0] or "image/png"
                b64 = base64.b64encode(f.read_bytes()).decode()
                imgs.append({"type": "image_url",
                             "image_url": {"url": f"data:{mime};base64,{b64}"}})
            else:
                try:
                    from .extract import clean_text, extract as _ex

                    text, why = _ex(f, max_bytes=8 * 1024 * 1024)
                    text = clean_text(text or "")
                    if text:
                        texts.append(f"【附件：{aid.split('_', 1)[-1]}】\n{text[:8000]}")
                    elif why:
                        texts.append(f"【附件：{aid} 没能读出来：{why}】")
                except Exception as exc:                        # noqa: BLE001
                    log.warning("读附件失败（%s）：%s", aid, exc)
        return imgs, texts

    # ---------- 会话 ----------
    def _own(conv_id: int, request) -> dict:                      # noqa: ANN001
        row = store._query(                                        # noqa: SLF001
            "SELECT * FROM chat_conversation WHERE id=?", (int(conv_id),))
        if not row:
            raise HTTPException(status_code=404, detail="没有这个会话")
        if int(row[0]["user_id"] or 0) != _uid(request):
            raise HTTPException(status_code=403, detail="这不是你的会话")
        return dict(row[0])

    @router.get("/api/chat2/conversations")
    async def list_convs(request: Request):
        uid = _uid(request)
        rows = store._query(                                     # noqa: SLF001
            "SELECT c.*, (SELECT COUNT(*) FROM chat_message m WHERE m.conv_id=c.id) AS n "
            "FROM chat_conversation c WHERE c.user_id=? AND c.archived=0 "
            "ORDER BY c.updated_at DESC", (uid,))
        return {"conversations": [dict(r) for r in rows]}

    @router.post("/api/chat2/conversations")
    async def new_conv(request: Request, payload: dict = Body(default={})):
        uid = _uid(request)
        now = time.time()
        title = str(payload.get("title") or "新对话").strip()[:MAX_TITLE]
        cur = store._conn.execute(                               # noqa: SLF001
            "INSERT INTO chat_conversation(user_id,title,model,created_at,updated_at) "
            "VALUES(?,?,?,?,?)", (uid, title, str(payload.get("model") or ""), now, now))
        store._conn.commit()                                     # noqa: SLF001
        return {"ok": True, "id": int(cur.lastrowid), "title": title}

    @router.put("/api/chat2/conversations/{conv_id}")
    async def rename_conv(request: Request, conv_id: int, payload: dict = Body(...)):
        _own(conv_id, request)
        title = str(payload.get("title") or "").strip()[:MAX_TITLE]
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空")
        store._conn.execute(                                     # noqa: SLF001
            "UPDATE chat_conversation SET title=?, updated_at=? WHERE id=?",
            (title, time.time(), int(conv_id)))
        store._conn.commit()                                     # noqa: SLF001
        return {"ok": True, "title": title}

    @router.delete("/api/chat2/conversations/{conv_id}")
    async def del_conv(request: Request, conv_id: int):
        _own(conv_id, request)
        store._conn.execute("DELETE FROM chat_message WHERE conv_id=?", (int(conv_id),))
        store._conn.execute("DELETE FROM chat_conversation WHERE id=?", (int(conv_id),))
        store._conn.commit()                                     # noqa: SLF001
        return {"ok": True}

    @router.get("/api/chat2/conversations/{conv_id}")
    async def get_conv(request: Request, conv_id: int):
        conv = _own(conv_id, request)
        rows = store._query(                                     # noqa: SLF001
            "SELECT id, role, content, sources, created_at FROM chat_message "
            "WHERE conv_id=? ORDER BY id", (int(conv_id),))
        msgs = []
        for r in rows:
            item = {"id": int(r["id"]), "role": r["role"], "content": r["content"],
                    "created_at": r["created_at"], "sources": None}
            if r["sources"]:
                try:
                    item["sources"] = json.loads(r["sources"])
                except Exception:                                # noqa: BLE001
                    item["sources"] = None
            msgs.append(item)
        return {"conversation": conv, "messages": msgs}

    # ---------- 流式发送 ----------
    @router.post("/api/chat2/conversations/{conv_id}/stream")
    async def stream_send(request: Request, conv_id: int, payload: dict = Body(...)):
        """和 /send 一样，但边生成边返回（SSE）。

        事件格式：
          data: {"delta": "文字"}   逐段
          data: {"done": true, ...} 结束（带 sources）
          data: {"error": "..."}    出错
        """
        from fastapi.responses import StreamingResponse
        from .vlm_client import chat_stream

        # 外网防刷：同 IP 每分钟最多 30 条（项目主 2026-10-21 外网部署）
        from . import ratelimit as _rl

        _ok, _wait = _rl.hit("chat", _rl.client_ip(request))
        if not _ok:
            raise HTTPException(status_code=429,
                                detail=f"发得太快了，请 {_wait} 秒后再试")
        conv = _own(conv_id, request)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="消息是空的")
        use_web = bool(payload.get("web"))
        use_kb = bool(payload.get("kb"))
        use_dify = bool(payload.get("dify"))
        pick = str(payload.get("pick") or "").strip()

        # 先算好要注入的上下文（这部分是同步的，放在流之前）
        web = None
        if use_web:
            _prov = str(cfg._get("search", "web_provider", "bing")).strip().lower()
            _sx = str(cfg._get("search", "searxng_url", "")).strip()
            web = websearch.search(text, 5, provider=_prov, searxng_url=_sx)
        dfy = None
        if use_dify:
            try:
                # 自己的 + 别人共享给我的（2026-10-22 加共享，两个入口要一致）✓
                from . import difyshare

                _me2 = _me(request)
                _dl = difyshare.usable(store, int(_me2.get("id") or 0),
                                       str(_me2.get("name") or ""))
            except Exception:                                    # noqa: BLE001
                _dl = []
            dfy = difyclient.retrieve_multi(_dl, text, 3)

        remote = None
        if pick.startswith("api:"):
            try:
                idx = int(pick.split(":", 1)[1])
                # 和 /api/chat2/models 同一个列表算下标（含公共/共享的那些）✓
                _items = _api_items_for(request)
                if idx < 0 or idx >= len(_items):
                    raise HTTPException(status_code=400,
                                        detail="这个模型已经不属于你了，刷新页面重选")
                item = _items[idx]
                base = str(item.get("base_url") or "").strip()
                mdl = str(item.get("model") or "").strip()
                if base and mdl:
                    # headers 可能是 JSON 字符串也可能是 dict → 统一解析（2026-10-23）
                    from . import apimodels as _am3

                    _hdr = _am3.headers_of(item)
                    if not _hdr:
                        _p3 = store.prefs_all(_uid(request), inherit_sensitive=True)
                        _hdr = _am3.headers_of({"headers": _p3.get("eg2-rmhdr")})
                    remote = {"base_url": base, "model": mdl,
                              "api_key": str(item.get("api_key") or ""),
                              "headers": _hdr, "max_tokens": 1200}
            except HTTPException:
                raise
            except Exception as exc:                             # noqa: BLE001
                log.warning("流式解析模型失败（%s）：%s", pick, exc)
        if remote is None:
            remote = _remote(request)
        if not remote:
            raise HTTPException(status_code=400,
                                detail="还没配外部模型：去「设置 → 外部模型」填好 ✓")

        # 存用户消息 + 起标题
        now = time.time()
        store._conn.execute(                                     # noqa: SLF001
            "INSERT INTO chat_message(conv_id,role,content,created_at) VALUES(?,?,?,?)",
            (int(conv_id), "user", text, now))
        n_msg = store._query(                                    # noqa: SLF001
            "SELECT COUNT(*) AS n FROM chat_message WHERE conv_id=?", (int(conv_id),))
        if int(n_msg[0]["n"]) <= 1 and (conv.get("title") or "新对话") == "新对话":
            store._conn.execute("UPDATE chat_conversation SET title=? WHERE id=?",  # noqa: SLF001
                                (text[:MAX_TITLE], int(conv_id)))
        store._conn.commit()                                     # noqa: SLF001

        hist = store._query(                                     # noqa: SLF001
            "SELECT role, content FROM chat_message WHERE conv_id=? "
            "ORDER BY id DESC LIMIT 12", (int(conv_id),))
        hist = list(reversed(hist))

        sys_hint = ("你是一个乐于助人的中文助手。回答要准确、简洁；"
                    "不确定就说不确定，不要编造。")
        ctx = websearch.as_context(web) if (web and web.get("ok")) else ""
        if ctx:
            sys_hint += "\n\n" + ctx + "\n引用网上资料时请标 [W1] [W2]。"
        dctx = difyclient.as_context_multi(dfy) if (dfy and dfy.get("ok")) else ""
        if dctx:
            sys_hint += "\n\n" + dctx
        # ★ 附件：图片走多模态块，文本/PDF 拼进 system prompt ✓
        _imgs, _texts = _load_attachments(request, payload.get("attachments"))
        if _texts:
            sys_hint += "\n\n" + "\n\n".join(_texts)

        # ★ 2026-10-23 项目主发现："聊天里勾了知识库，什么标记都没有"
        #   根因：聊天页走的是**流式**接口，而流式这条分支**根本没做知识库检索** ——
        #   `use_kb` 读了却没用，等于勾了个寂寞 ✗（非流式的 /send 里是有的）
        #   这里补上：检索 → 拼 [K1][K2] 上下文 → 把命中片段放进 sources 返回给界面 ✓
        kb_hits: list[dict] = []
        kb_note = ""
        if use_kb:
            try:
                _sc = searcher()
                if _sc is None:
                    kb_note = "知识库不可用（索引没加载）"
                else:
                    from .multiuser import visible_folder_ids as _vis

                    vis_ids = _vis(cfg.db_path, _me_user(request))
                    res = _sc.search(text, top_k=8, folder_ids=vis_ids)
                    # ⚠ 2026-10-23：Searcher.search 返回的键是 **results**（不是 hits）——
                    #   以前这里读 hits 永远拿到 None，于是聊天里勾了知识库
                    #   一条都搜不到、也没有任何标记 ✗
                    rows = ((res.get("results") or res.get("hits"))
                            if isinstance(res, dict) else res) or []
                    for h in rows[:6]:
                        kb_hits.append({
                            "file": h.get("name") or h.get("path") or "",
                            "snippet": (h.get("text") or h.get("snippet") or "")[:900],
                            "score": round(float(h.get("score") or 0), 3),
                        })
                    if kb_hits:
                        lines = ["以下是用户自己知识库里相关的片段"
                                 "（引用时标 [K1] [K2]）："]
                        for i, h in enumerate(kb_hits, 1):
                            lines.append(f"[K{i}] {h['file']}\n{h['snippet']}")
                        sys_hint += "\n\n" + "\n\n".join(lines)
                    else:
                        kb_note = "知识库里没检索到相关内容"
            except Exception as exc:                             # noqa: BLE001
                log.warning("聊天（流式）检索知识库失败：%s", exc)
                kb_note = f"知识库检索失败：{exc}"

        messages = [{"role": "system", "content": sys_hint}]
        for r in hist[:-1]:                  # 最后一条（刚发的）单独处理，好挂图片
            messages.append({"role": r["role"], "content": r["content"]})
        if _imgs:
            # OpenAI 兼容的多模态格式：文本 + 若干 image_url
            messages.append({"role": "user", "content":
                             [{"type": "text", "text": text}] + _imgs})
        else:
            messages.append({"role": "user", "content": text})

        src = {"web": (web.get("results") if web else []),
               "web_error": (web.get("error") if web else ""),
               # ★ 2026-10-23：把知识库 / Dify 的命中一起存下来，
               #   这样刷新页面、导出对话都还能看到"参考了什么" ✓
               "kb": kb_hits, "kb_note": kb_note,
               "dify": ((dfy or {}).get("results") if isinstance(dfy, dict) else []) or []}

        def gen():
            parts: list[str] = []
            try:
                for piece in chat_stream(remote.get("base_url"), remote.get("model"),
                                         messages, timeout=180,
                                         max_tokens=int(remote.get("max_tokens") or 1200),
                                         api_key=remote.get("api_key") or None,
                                         extra_headers=remote.get("headers") or None):
                    parts.append(piece)
                    yield "data: " + json.dumps({"delta": piece}, ensure_ascii=False) + "\n\n"
            except Exception as exc:                             # noqa: BLE001
                log.warning("流式生成失败：%s", exc)
                yield "data: " + json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n\n"
                return
            answer = strip_think("".join(parts))
            # 存库（和 /send 一致，这样刷新后还能看到）
            try:
                store._conn.execute(                         # noqa: SLF001
                    "INSERT INTO chat_message(conv_id,role,content,sources,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (int(conv_id), "assistant", answer,
                     json.dumps(src, ensure_ascii=False), time.time()))
                store._conn.execute("UPDATE chat_conversation SET updated_at=? WHERE id=?",  # noqa: SLF001
                                    (time.time(), int(conv_id)))
                store._conn.commit()                         # noqa: SLF001
            except Exception as exc:                         # noqa: BLE001
                log.warning("流式存库失败：%s", exc)
            yield "data: " + json.dumps({"done": True, "web": web,
                                         "kb": kb_hits, "kb_note": kb_note,
                                         "dify": (dfy or {}).get("results") or [],
                                         "model": remote.get("model") or ""},
                                        ensure_ascii=False) + "\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---------- 导出 ----------
    @router.get("/api/chat2/conversations/{conv_id}/export")
    async def export_conv(request: Request, conv_id: int,
                          fmt: str = Query("md")):
        """导出成一个文件（Markdown 或 JSON）。前端直接下载。"""
        conv = _own(conv_id, request)
        rows = store._query(                                     # noqa: SLF001
            "SELECT role, content, sources, created_at FROM chat_message "
            "WHERE conv_id=? ORDER BY id", (int(conv_id),))
        title = str(conv.get("title") or "对话")
        safe = "".join(c for c in title if c not in '\\/:*?"<>|')[:40] or "对话"

        if (fmt or "").lower() == "json":
            payload = {"title": title, "created_at": conv.get("created_at"),
                       "messages": [
                           {"role": r["role"], "content": r["content"],
                            "created_at": r["created_at"],
                            "sources": (json.loads(r["sources"]) if r["sources"] else None)}
                           for r in rows]}
            body = json.dumps(payload, ensure_ascii=False, indent=2)
            media, ext = "application/json; charset=utf-8", "json"
        else:
            lines = [f"# {title}", ""]
            for r in rows:
                who = "我" if r["role"] == "user" else "助手"
                lines.append(f"**{who}**")
                lines.append("")
                lines.append(str(r["content"]))
                if r["sources"]:
                    try:
                        s = json.loads(r["sources"])
                    except Exception:                            # noqa: BLE001
                        s = {}
                    for i, x in enumerate(s.get("web") or [], 1):
                        lines.append(f"> [W{i}] {x.get('title', '')} — {x.get('url', '')}")
                    for i, x in enumerate(s.get("kb") or [], 1):
                        lines.append(f"> [K{i}] {x.get('file', '')}")
                lines.append("")
                lines.append("---")
                lines.append("")
            body = "\n".join(lines)
            media, ext = "text/markdown; charset=utf-8", "md"

        from urllib.parse import quote as _q

        fname = f"{safe}.{ext}"
        from fastapi import Response

        return Response(content=body.encode("utf-8"), media_type=media,
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{_q(fname)}"})

    # ---------- 可选模型列表 ----------
    @router.get("/api/chat2/models")
    async def list_models(request: Request):
        """已部署的本机模型 + 用户配的多套 API。"""
        uid = _uid(request)
        out: list[dict] = []
        # ① 本机已部署的模型由前端去 `/api/chat/models` 拿（那个接口在 filesearch 里，
        #    依赖它自己的闭包，这里调不到）。这边只负责列 **API 那几套** ✓
        # ② 用户配的多套 API（自己 + 别人公共/共享给我的）
        try:
            items = _api_items_for(request)
            for i, x in enumerate(items):
                nm = str(x.get("name") or f"API {i + 1}")
                md = str(x.get("model") or "")
                if not md or not str(x.get("base_url") or "").strip():
                    continue
                _tag = ""
                if x.get("_own"):
                    _tag = "" if not x.get("share") else "（已共享）"
                elif x.get("_public"):
                    _tag = "（公共）"
                else:
                    _tag = "（" + str(x.get("_owner") or "") + " 共享）"
                out.append({"id": f"api:{i}",
                            "label": f"{nm} · {md}{_tag}",
                            "kind": "api",
                            "shared": (not x.get("_own")),
                            "public": bool(x.get("_public")),
                            "owner": str(x.get("_owner") or ""),
                            "vl": looks_vl(md)})
            # 单选落空的兜底：老版本只有一套配置（eg2-rmurl / rmmodel）
            if not any(o["kind"] == "api" for o in out):
                prefs0 = store.prefs_all(_uid(request), inherit_sensitive=True)
                u0 = str(prefs0.get("eg2-rmurl") or "")
                m0 = str(prefs0.get("eg2-rmmodel") or "")
                if u0 and m0:
                    out.append({"id": "api:0", "label": f"当前配置 · {m0}",
                                "kind": "api"})
        except Exception as exc:                                 # noqa: BLE001
            log.warning("列 API 配置失败：%s", exc)
        return {"models": out}

    # ---------- 联网搜索（单独一个接口，前端可以先试）----------
    @router.post("/api/chat2/search")
    async def do_search(request: Request, payload: dict = Body(...)):
        _uid(request)                                            # 只要求登录
        q = str(payload.get("q") or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="搜索词是空的")
        _prov = str(cfg._get("search", "web_provider", "bing")).strip().lower()
        _sx = str(cfg._get("search", "searxng_url", "")).strip()
        return websearch.search(q, int(payload.get("count") or 5),
                                provider=_prov, searxng_url=_sx)

    # ---------- 发消息 ----------
    @router.post("/api/chat2/conversations/{conv_id}/send")
    async def send(request: Request, conv_id: int, payload: dict = Body(...)):
        # 外网防刷：同 IP 每分钟最多 30 条（项目主 2026-10-21 外网部署）
        from . import ratelimit as _rl

        _ok, _wait = _rl.hit("chat", _rl.client_ip(request))
        if not _ok:
            raise HTTPException(status_code=429,
                                detail=f"发得太快了，请 {_wait} 秒后再试")
        conv = _own(conv_id, request)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="消息是空的")
        use_web = bool(payload.get("web"))
        use_kb = bool(payload.get("kb"))
        use_dify = bool(payload.get("dify"))

        now = time.time()
        # ① 存用户消息
        store._conn.execute(                                     # noqa: SLF001
            "INSERT INTO chat_message(conv_id,role,content,created_at) VALUES(?,?,?,?)",
            (int(conv_id), "user", text, now))
        # ② 首条消息拿来当标题（省得用户自己起）
        n_msg = store._query(                                    # noqa: SLF001
            "SELECT COUNT(*) AS n FROM chat_message WHERE conv_id=?", (int(conv_id),))
        if int(n_msg[0]["n"]) <= 1 and (conv.get("title") or "新对话") == "新对话":
            store._conn.execute(                                 # noqa: SLF001
                "UPDATE chat_conversation SET title=? WHERE id=?",
                (text[:MAX_TITLE], int(conv_id)))
        store._conn.commit()                                     # noqa: SLF001

        # ③ 联网（可选）
        # 搜索源可在 config.ini 里配（默认 bing 抓 HTML）
        _prov = str(cfg._get("search", "web_provider", "bing")).strip().lower()
        _sx = str(cfg._get("search", "searxng_url", "")).strip()
        web = (websearch.search(text, 5, provider=_prov, searxng_url=_sx)
               if use_web else None)
        # ③b Dify 知识库（自己的 + 别人共享给我的，2026-10-22 加了共享）
        dfy = None
        if use_dify:
            try:
                from . import difyshare

                _me = _me(request)
                _dl = difyshare.usable(store, int(_me.get("id") or 0),
                                       str(_me.get("name") or ""))
            except Exception:                                    # noqa: BLE001
                _dl = []
            dfy = difyclient.retrieve_multi(_dl, text, 3)

        # ④ 历史（最近 12 条，含刚存的这条）
        hist = store._query(                                     # noqa: SLF001
            "SELECT role, content FROM chat_message WHERE conv_id=? "
            "ORDER BY id DESC LIMIT 12", (int(conv_id),))
        hist = list(reversed(hist))

        sys_hint = ("你是一个乐于助人的中文助手。回答要准确、简洁；"
                    "不确定就说不确定，不要编造。")
        ctx = websearch.as_context(web) if (web and web.get("ok")) else ""
        if ctx:
            sys_hint += "\n\n" + ctx + "\n引用网上资料时请标 [W1] [W2]。"
        dctx = difyclient.as_context_multi(dfy) if (dfy and dfy.get("ok")) else ""
        if dctx:
            sys_hint += "\n\n" + dctx
        kb_hits: list[dict] = []
        kb_note = ""
        if use_kb:
            try:
                _sc = searcher()
                if _sc is None:
                    kb_note = "知识库不可用（索引没加载）"
                else:
                    # 只在他能看的文件夹里检索（多人隔离）
                    from .multiuser import visible_folder_ids as _vis

                    _me = _prefs(request)                     # 触发一次连接（无副作用）
                    vis_ids = _vis(cfg.db_path, _me_user(request))
                    res = _sc.search(text, top_k=8, folder_ids=vis_ids)
                    # ⚠ 2026-10-23：Searcher.search 返回的键是 **results**（不是 hits）——
                    #   以前这里读 hits 永远拿到 None，于是聊天里勾了知识库
                    #   一条都搜不到、也没有任何标记 ✗
                    rows = ((res.get("results") or res.get("hits"))
                            if isinstance(res, dict) else res) or []
                    for h in rows[:6]:
                        kb_hits.append({
                            "file": h.get("name") or h.get("path") or "",
                            "snippet": (h.get("text") or h.get("snippet") or "")[:900],
                            "score": round(float(h.get("score") or 0), 3),
                        })
                    if kb_hits:
                        lines = ["以下是用户自己知识库里相关的片段"
                                 "（引用时标 [K1] [K2]）："]
                        for i, h in enumerate(kb_hits, 1):
                            lines.append(f"[K{i}] {h['file']}\n{h['snippet']}")
                        sys_hint += "\n\n" + "\n\n".join(lines)
                    else:
                        kb_note = "知识库里没检索到相关内容"
            except Exception as exc:                             # noqa: BLE001
                log.warning("聊天检索知识库失败：%s", exc)
                kb_note = f"知识库检索失败：{exc}"

        _imgs, _texts = _load_attachments(request, payload.get("attachments"))
        if _texts:
            sys_hint += "\n\n" + "\n\n".join(_texts)
        messages = [{"role": "system", "content": sys_hint}]
        for r in hist[:-1]:
            messages.append({"role": r["role"], "content": r["content"]})
        if _imgs:
            messages.append({"role": "user", "content":
                             [{"type": "text", "text": text}] + _imgs})
        else:
            messages.append({"role": "user", "content": text})

        # ⑤ 调模型
        # ★ 模型选择：api:N 用 rmapilist 里那一套；local:... 用本机模型
        pick = str(payload.get("pick") or "").strip()
        remote = None
        if pick.startswith("api:"):
            try:
                idx = int(pick.split(":", 1)[1])
                # ★ 2026-10-22：和 /api/chat2/models 用**同一个**列表算下标，
                #   否则普通用户选「服务器 llama.cpp」会因为下标对不上而报"没共享给你" ✗
                items = _api_items_for(request)
                if idx < 0 or idx >= len(items):
                    raise HTTPException(status_code=400, detail="这个模型已经不属于你了，刷新页面重选")
                item = items[idx]
                base = str(item.get("base_url") or "").strip()
                mdl = str(item.get("model") or "").strip()
                if base and mdl:
                    # ⚠ 必须带全局的额外 header：opencode 那套要靠
                    #   x-opencode-session 才认，不带给就是 502 ✗（实测栽过）
                    #   2026-10-23：headers 在偏好里可能是 JSON 字符串、也可能是 dict，
                    #   统一交给 apimodels.headers_of 解析（别再自己 json.loads(str(dict))）
                    from . import apimodels as _am2

                    _hdr = _am2.headers_of(item)
                    if not _hdr:
                        _p2 = store.prefs_all(_uid(request), inherit_sensitive=True)
                        _hdr = _am2.headers_of({"headers": _p2.get("eg2-rmhdr")})
                    remote = {"base_url": base, "model": mdl,
                              "api_key": str(item.get("api_key") or ""),
                              "headers": _hdr, "max_tokens": 1200}
            except HTTPException:
                raise
            except Exception as exc:                             # noqa: BLE001
                log.warning("解析模型选择失败（%s）：%s", pick, exc)
        if remote is None:
            remote = _remote(request)
        if not remote:
            raise HTTPException(
                status_code=400,
                detail="还没配外部模型：去「设置 → 外部模型」填地址 + 模型名 + Key ✓")

        from .vlm_client import chat_once

        try:
            answer = chat_once(remote.get("base_url"), remote.get("model"), messages,
                               timeout=180,
                               max_tokens=int(remote.get("max_tokens") or 1200),
                               api_key=remote.get("api_key") or None,
                               extra_headers=remote.get("headers") or None)
        except Exception as exc:                                 # noqa: BLE001
            log.warning("聊天调用模型失败：%s", exc)
            raise HTTPException(status_code=502, detail=f"模型调用失败：{exc}") from exc

        # ★ 存库前剥掉思考过程（否则界面上全是"让我想想…"）
        answer = strip_think(answer)

        # ⑥ 存回答（带联网来源）
        src = {"web": (web.get("results") if web else []),
               "web_error": (web.get("error") if web else ""),
               "kb": kb_hits, "kb_note": kb_note, "dify": dfy}
        store._conn.execute(                                     # noqa: SLF001
            "INSERT INTO chat_message(conv_id,role,content,sources,created_at) "
            "VALUES(?,?,?,?,?)",
            (int(conv_id), "assistant", answer,
             json.dumps(src, ensure_ascii=False), time.time()))
        store._conn.execute(                                     # noqa: SLF001
            "UPDATE chat_conversation SET updated_at=? WHERE id=?",
            (time.time(), int(conv_id)))
        store._conn.commit()                                     # noqa: SLF001

        return {"ok": True, "answer": answer, "web": web,
                "model": (remote or {}).get("model") or "",
                "kb": kb_hits, "kb_note": kb_note,
                "dify": dfy,
                "title": (store._query(                          # noqa: SLF001
                    "SELECT title FROM chat_conversation WHERE id=?",
                    (int(conv_id),))[0]["title"])}

    return router
