"""用 VL 模型给图片生成文字描述，让图片能被中文搜到。

为什么需要
----------
剪藏文章的插图文件名是哈希（506ce0c5..._MD5.jpg），一个中文字都没有，
文字搜索永远找不到它们。VL 模型能"看到"图，把内容写成中文描述存进库，
这些图就变成可搜索的了。

显存安排
--------
8 GB 卡上：嵌入模型 2.3 GB + VL 3B 7.5 GB = 9.8 GB，放不下。
所以打标开始前把嵌入模型挪到 CPU（腾出 2.3 GB），
打标完成后挪回显卡。打标期间搜索会慢一点，但不会出错。

落库方式
--------
生成的描述写进 chunks 表（kind='caption'），并同步算一个向量，
这样景象搜索和文字搜索都能命中。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("eg2.caption")

PROMPT = (
    "用一句中文描述这张图片的内容，20-60 字。"
    "如果图里有文字，把关键文字也写出来。"
    "只输出描述本身，不要任何前缀、解释或标点开头的客套话。"
)

# 状态
_state = {
    "running": False, "done": 0, "total": 0, "errors": 0,
    "started": 0.0, "elapsed": 0.0, "current": "", "message": "空闲",
    "skipped": 0,
    # 时段控制：只在 [start_hour, end_hour) 之间干活，其余时间暂停。
    # 不设就是全天可跑。
    "window": None,          # 形如 [22, 7] 表示 22:00 ~ 次日 7:00
    "paused": False,         # 当前是不是因为不在时段内而暂停
    "model": "", "model_kind": "local", "vram_limit": 0,
}
_lock = threading.Lock()


def status() -> dict:
    with _lock:
        s = dict(_state)
    if s["running"] and s["started"]:
        s["elapsed"] = time.time() - s["started"]
    return s


def stop() -> None:
    with _lock:
        _state["running"] = False
        _state["message"] = "正在停止…"


def _in_window(window) -> bool:
    """现在是否落在允许运行的时段内。window 形如 [22, 7]。"""
    if not window:
        return True
    import datetime as _dt
    h = _dt.datetime.now().hour
    a, b = int(window[0]), int(window[1])
    if a == b:
        return True
    if a < b:
        return a <= h < b
    return h >= a or h < b          # 跨零点，比如 22 → 7


def start(cfg, store, engine, chat_engine, limit: int = 0,
          folder_ids: list[int] | None = None, max_side: int = 768,
          move_embedding=None, window=None, vl_dir=None,
          vram_limit: int = 0, remote: dict | None = None) -> dict:
    """启动后台打标线程。返回 {"started": bool, "reason": str}。"""
    with _lock:
        if _state["running"]:
            return {"started": False, "reason": "已经在跑了"}
        _state.update(model=(str(remote.get("model")) if remote
                             else (pathlib.Path(vl_dir).name if vl_dir else "本机 VL")),
                      model_kind=("remote" if remote else "local"),
                      vram_limit=int(vram_limit or 0))
        _state.update(running=True, done=0, total=0, errors=0, skipped=0,
                      started=time.time(), elapsed=0.0, current="",
                      message="准备中…", window=window, paused=False)
    t = threading.Thread(
        target=_run, args=(cfg, store, engine, chat_engine, limit,
                           folder_ids, max_side, move_embedding, window,
                           vl_dir, vram_limit, remote),
        daemon=True, name="vl-caption")
    t.start()
    return {"started": True, "reason": ""}


def _run(cfg, store, engine, chat_engine, limit, folder_ids, max_side,
         move_embedding=None, window=None, vl_dir=None,
         vram_limit: int = 0, remote: dict | None = None) -> None:
    from PIL import Image

    from .model import decode_image
    import numpy as np

    moved_to_cpu = False
    try:
        # ---- 收集要打标的图片：kind=image，且还没有 caption 片段 ----
        where = ["f.kind = 'image'"]
        args: list = []
        if folder_ids:
            where.append(f"f.folder_id IN ({','.join('?' * len(folder_ids))})")
            args += [int(x) for x in folder_ids]
        where.append("NOT EXISTS (SELECT 1 FROM chunks c WHERE c.file_id = f.id "
                     "AND c.kind = 'caption')")
        sql = ("SELECT f.id, f.path, f.name FROM files f WHERE "
               + " AND ".join(where) + " ORDER BY f.id")
        rows = store._query(sql, args)
        # 排除已知坏图（只含解码失败的，网络/模型错误不在这里）
        try:
            store.ensure_caption_skip()
            _skip = store.caption_skip_ids()
        except Exception:
            _skip = set()
        if _skip:
            rows = [r for r in rows if int(r['id']) not in _skip]
            log.info('跳过 %d 张已知坏图（换模型也读不了，可在界面清空）',
                     len(_skip))
        if limit:
            rows = rows[:int(limit)]
        with _lock:
            _state["total"] = len(rows)
            _state["message"] = f"共 {len(rows)} 张待打标"

        if not rows:
            with _lock:
                _state.update(running=False, message="没有需要打标的图片")
            return

        # ---- 腾显存：嵌入模型挪到 CPU ----
        if move_embedding is not None:
            try:
                move_embedding("cpu")
                moved_to_cpu = True
                log.info("打标开始：嵌入模型已挪到 CPU 腾显存")
            except Exception as exc:                          # noqa: BLE001
                log.warning("挪嵌入模型失败：%s", exc)

        # ---- 加载 VL ----
        # 关键：必须切到 **VL 模型** 的目录。
        # 用 chat_engine.model_dir 会切到配置里的文本模型（1.5B），
        # 它看不到图，只会对每张图瞎编同一段描述（实测踩过）。
        remote_fn = None
        if remote:
            from .vlm_client import caption_one as _rc

            _u = str(remote.get("base_url") or "")
            _m = str(remote.get("model") or "")
            _k = str(remote.get("api_key") or "")
            _h = remote.get("headers") or None
            _s = int(remote.get("max_side") or 768)
            _tk = int(remote.get("max_tokens") or 200)
            # ★ 推理型模型要关掉思考，否则正文全是草稿（详见 caption_run 里的说明）
            from .caption_run import caption_extra_body as _eb_of
            _eb = _eb_of(_m)
            log.info("打标使用外部模型: %s @ %s", _m, _u)

            def remote_fn(im):                                    # noqa: ANN001
                # ⚠ 2026-10-20：1800 秒太长了（一张卡住整批停摆，见 caption_run 里的说明）
                return _rc(_u, _m, im, timeout=180, max_side=_s,
                           max_tokens=_tk, api_key=_k, extra_headers=_h,
                           extra_body=_eb)
        else:
            target = str(vl_dir) if vl_dir else str(chat_engine.model_dir)
            chat_engine.switch_to(target)
            try:
                chat_engine.set_vram_limit(vram_limit)
            except Exception as exc:                              # noqa: BLE001
                log.warning("设显存上限失败：%s", exc)
            log.info("打标使用本机模型: %s（显存上限 %s MB）",
                     target, vram_limit or "不限")
            ok, why = (chat_engine.available()
                       if hasattr(chat_engine, "available") else (True, ""))
            if not ok:
                with _lock:
                    _state.update(running=False, message=f"VL 不可用：{why}")
                return

        for n, r in enumerate(rows, 1):
            # 时段控制：不在允许时段内就挂起等待，不退出任务
            while not _in_window(window):
                with _lock:
                    if not _state["running"]:
                        _state["message"] = "已手动停止"
                        return
                    _state["paused"] = True
                    _state["message"] = (f"已到暂停时段（{window[0]}:00-"
                                         f"{window[1]}:00 之外），等下一个窗口")
                    _state["done"] = n - 1
                time.sleep(60)
            with _lock:
                _state["paused"] = False
                if not _state["running"]:
                    _state["message"] = "已手动停止"
                    break
                _state["done"] = n - 1
                _state["current"] = str(r["name"])[:60]
            try:
                im = decode_image(Path(r["path"]), max_side=max_side)
                if im.mode != "RGB":
                    im = im.convert("RGB")
                if remote_fn is not None:
                    cap = str(remote_fn(im) or "").strip()
                else:
                    out = chat_engine.chat(
                        [{"role": "user", "content": PROMPT}],
                        max_new_tokens=120, temperature=0.2, images=[im])
                    cap = (out.get("answer") or "").strip()
                cap = cap.replace("\n", " ").strip()[:300]
                if len(cap) < 6:
                    with _lock:
                        _state["skipped"] += 1
                    continue
                _save(store, engine, int(r["id"]), cap)
            except Exception as exc:                          # noqa: BLE001
                msg = str(exc)
                with _lock:
                    _state["errors"] += 1
                # 严格区分两类失败：
                #   解码失败（没走到网络）→ 永久跳过，换模型也读不了
                #   网络/模型/额度错误    → 不记，下次照样重试
                _bad = any(k in msg for k in (
                    "无法识别的图片格式", "无法解码", "解码失败",
                    "cannot identify image", "unidentified image"))
                if _bad:
                    try:
                        store.caption_skip_add(int(r["id"]), msg)
                    except Exception:                         # noqa: BLE001
                        pass
                    log.warning("坏图已跳过（解码失败）%s：%s", r["name"], msg)
                else:
                    log.warning("打标失败（下次会重试）%s：%s", r["name"], msg)
        with _lock:
            _state["done"] = _state["total"]
            _state["running"] = False
            _state["message"] = (f"完成：{_state['done']} 张，"
                                 f"跳过 {_state['skipped']}，失败 {_state['errors']}")
    except Exception as exc:                                  # noqa: BLE001
        log.exception("打标任务异常")
        with _lock:
            _state.update(running=False, message=f"异常：{exc}")
    finally:
        try:
            chat_engine.unload()
        except Exception:                                     # noqa: BLE001
            pass
        if moved_to_cpu and move_embedding is not None:
            try:
                move_embedding("cuda")
                log.info("打标结束：嵌入模型已挪回显卡")
            except Exception as exc:                          # noqa: BLE001
                log.warning("挪回显卡失败：%s", exc)


def _save(store, engine, file_id: int, caption: str) -> None:
    """把描述写成一条 caption 片段，并算好向量。"""
    import numpy as np

    vec = None
    if engine is not None and getattr(engine, "model", None) is not None:
        try:
            # 必须和文字片段用同一个 prompt（indexer 里是 "Document"），
            # 否则两者不在同一向量空间，知识库检索根本排不上来。
            _dim = getattr(getattr(store, "cfg", None), "index_dim", None)
            v = engine.encode([caption], prompt_name="Document",
                              dim=_dim)[0]
            v = np.asarray(v, dtype=np.float32)
            n = float(np.linalg.norm(v))
            if n > 1e-6:
                vec = (v / n).astype(np.float32).tobytes()
        except Exception:                                     # noqa: BLE001
            vec = None
    seq = store._query(
        "SELECT COALESCE(MAX(seq), -1) + 1 s FROM chunks WHERE file_id=?",
        (file_id,))[0]["s"]
    with store._conn:
        from . import ftsindex as _fts                              # noqa: PLC0415

        store._conn.execute(
            "INSERT INTO chunks(file_id, seq, kind, text, vec, dim, uni) "
            "VALUES(?,?,?,?,?,?,?)",
            (file_id, int(seq), "caption", caption, vec,
             int(len(vec) // 4) if vec else 0, _fts.unigrams(caption)))
        store._conn.execute(
            "UPDATE files SET n_chunks = (SELECT COUNT(*) FROM chunks "
            "WHERE file_id = ?) WHERE id = ?", (file_id, file_id))
