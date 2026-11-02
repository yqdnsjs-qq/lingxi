"""本机 → 服务器 的上传执行器（带进度，供界面按钮用）。

和 `run/upload_folders.py` 的关系
--------------------------------
那个是**命令行**版（适合脚本/定时）；这里是**界面按钮**版，多了：
  · 后台线程跑（不卡界面）+ 进度（打包了多少 MB / 传到第几段）
  · 服务器账号和地址从 `config.ini` 的 `[sync]` 读，界面能改 ✓

流程（每一步都能单独失败、并且说清楚为什么）
--------------------------------------------
  ① 打包：`app/foldertransfer.export_folders()`（只导 768 维，异构跳过）
  ② 登录：用 `[sync] user / password` 换会话（不把 token 长期存盘）
  ③ 上传：流式 POST 到 `[sync] server` 的 `/api/sync/folder-upload`
  ④ 汇总：把服务器返回的统计原样带回来（新建多少文件夹/文件/片段）

⚠ 预演（dry_run）会走完 ①②③，只是不让服务器写库 —— 这是最安全的第一次点 ✓
"""
from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("eg2.syncpush")

DEFAULT_SERVER = "http://192.168.1.10:8199"

_STATE: dict = {
    "running": False, "phase": "", "note": "", "error": "",
    "started": 0.0, "finished": 0.0, "dry_run": False,
    "pack_mb": 0.0, "folders": 0, "files": 0, "chunks": 0,
    "result": None, "server": "",
}
_LOCK = threading.RLock()


def status() -> dict:
    with _LOCK:
        return dict(_STATE)


def _set(**kw) -> None:                                          # noqa: ANN003
    with _LOCK:
        _STATE.update(kw)


_PICK_CACHE: dict = {"at": 0.0, "base": "", "cands": []}
_PICK_TTL = 120.0          # 探测结果缓存 120 秒（切网络后最多等两分钟）
_PICK_TIMEOUT = 2.0        # 单个地址探测超时（内网 2 秒连不上基本就是不在同一个网）


def servers(cfg) -> list[str]:                                   # noqa: ANN001
    """解析 `[sync] server` 里的所有候选地址（按顺序）。

    支持逗号、分号、换行分隔；没写协议头自动补 http://
    """
    raw = ""
    try:
        raw = str(cfg._get("sync", "server", "") or "")
    except Exception:                                            # noqa: BLE001
        raw = ""
    out: list[str] = []
    for piece in raw.replace(";", ",").replace("\n", ",").split(","):
        u = piece.strip().rstrip("/")
        if not u:
            continue
        if not u.startswith(("http://", "https://")):
            u = "http://" + u
        if u not in out:
            out.append(u)
    return out or [DEFAULT_SERVER]


def _probe(base: str) -> bool:
    """这个地址连不连得上（只看 /health）。"""
    import urllib.request

    try:
        req = urllib.request.Request(base.rstrip("/") + "/health",
                                     headers={"User-Agent": "eg2-sync"})
        with urllib.request.urlopen(req, timeout=_PICK_TIMEOUT) as r:
            return 200 <= int(r.status) < 300
    except Exception:                                            # noqa: BLE001
        return False


def pick_server(cfg, force: bool = False) -> str:                 # noqa: ANN001
    """挑一个当前能用的服务器地址：**内网优先，外网兜底**。

    结果缓存 120 秒；`force=True` 立刻重探（设置页"试连"用它）。
    """
    import time as _t

    cands = servers(cfg)
    now = _t.time()
    if (not force and _PICK_CACHE["cands"] == cands
            and now - _PICK_CACHE["at"] < _PICK_TTL and _PICK_CACHE["base"]):
        return str(_PICK_CACHE["base"])
    picked = ""
    for base in cands:
        if _probe(base):
            picked = base
            break
    if not picked:
        picked = cands[0]                                        # 都不通，用第一个
        log.warning("所有服务器地址都连不上（%s），暂用第一个", "、".join(cands))
    else:
        log.info("同步目标：%s（候选 %d 个）", picked, len(cands))
    _PICK_CACHE.update({"at": now, "base": picked, "cands": cands})
    return picked


def candidates(cfg) -> list[dict]:                               # noqa: ANN001
    """给界面用：每个候选地址 + 现在通不通。"""
    out = []
    for base in servers(cfg):
        out.append({"server": base, "ok": _probe(base)})
    return out


def account(cfg) -> dict:                                        # noqa: ANN001
    """读服务器地址和账号（密码不回显，只告诉"设没设"）✓"""
    def g(key: str, default: str = "") -> str:
        try:
            return str(cfg._get("sync", key, default) or "")
        except Exception:                                        # noqa: BLE001
            return default

    pwd = g("password")
    tok = g("token")
    try:
        base = pick_server(cfg)              # ★ 自适应：内网优先、外网兜底
    except Exception:                                            # noqa: BLE001
        base = g("server", DEFAULT_SERVER) or DEFAULT_SERVER
    return {"server": base,
            "servers": servers(cfg),         # 全部候选（界面展示用）
            "user": g("user"), "has_password": bool(pwd),
            "has_token": bool(tok)}


def login(base: str, user: str, password: str) -> str:
    import urllib.request
    # ⚠ 2026-10-22 抓到的老 bug：登录接口收的字段名是 **pwd**（auth_api.py 的
    #   `payload.get("pwd")`），这里一直发的是 `password` → 服务器永远收不到密码，
    #   于是"账号 + 密码"这条路**从来没成功过**，只有手填 token 的才通 ✗
    #   （项目主填完账号密码点验证，只会看到"账号或密码不对"，非常像密码输错）
    #   两个名字一起发：新老版本服务器都认 ✓
    body = json.dumps({"name": user, "pwd": password,
                       "password": password}).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + "/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
        raw = resp.headers.get("Set-Cookie") or ""
    tok = str(data.get("token") or "")
    if not tok:
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("eg2_sid="):
                tok = part.split("=", 1)[1]
                break
    if not tok:
        raise RuntimeError("登录没拿到会话（账号或密码不对？）")
    return tok


def post_pack(base: str, token: str, pack: Path, dry_run: bool) -> dict:
    """流式上传（不把整个包读进内存）✓"""
    u = urlparse(base)
    cls = (http.client.HTTPSConnection if u.scheme == "https"
           else http.client.HTTPConnection)
    conn = cls(u.hostname, u.port, timeout=1800)
    path = (u.path or "").rstrip("/") + "/api/sync/folder-upload"
    if dry_run:
        path += "?dry_run=true"
    size = pack.stat().st_size
    conn.putrequest("POST", path)
    conn.putheader("Cookie", "eg2_sid=" + token)
    conn.putheader("Content-Type", "application/gzip")
    conn.putheader("Content-Length", str(size))
    conn.endheaders()
    sent = 0
    with pack.open("rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            conn.send(chunk)
            sent += len(chunk)
            pct = int(sent * 100 / max(size, 1))
            _set(phase="上传中", note=f"{sent/1e6:.1f} / {size/1e6:.1f} MB（{pct}%）")
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", "replace")
    conn.close()
    if resp.status >= 400:
        raise RuntimeError(f"服务器返回 {resp.status}：{raw[:300]}")
    return json.loads(raw)


def run(cfg, store, folder_ids: list[int], dry_run: bool = True) -> dict:  # noqa: ANN001
    """真跑一次（阻塞，供后台线程调用）✓"""
    from . import foldertransfer as FT

    acct = account(cfg)
    base = acct["server"]
    _set(running=True, phase="打包", note="正在打包…", error="", result=None,
         started=time.time(), finished=0.0, dry_run=dry_run, server=base,
         pack_mb=0.0, folders=len(folder_ids), files=0, chunks=0)
    pack = None
    try:
        if not acct["has_token"] and (not acct["user"] or not acct["has_password"]):
            raise RuntimeError(
                "还没设服务器凭据：填「账号 / 密码」保存，或者填一个会话 token")
        pack = Path(cfg.tmp_dir) / f"push-{int(time.time()*1000)}.jsonl.gz"
        stat = FT.export_folders(store, folder_ids, pack, note="界面推送",
                                 dim=cfg.index_dim)
        _set(pack_mb=round(stat["bytes"] / 1e6, 2), files=stat["files"],
             chunks=stat["chunks"],
             note=f"包好了：{stat['files']} 个文件 / {stat['chunks']} 个片段")
        _set(phase="登录", note="正在登录服务器…")
        # 优先用手填的 token（本机与服务器同步过同一份库时，会话本来就通用 ✓）
        tok = str(cfg._get("sync", "token", "") or "").strip()
        if not tok:
            try:
                tok = login(base, acct["user"], cfg._get("sync", "password", ""))
            except Exception:                                    # noqa: BLE001
                tok = ""
        if not tok:
            raise RuntimeError("登录服务器失败（账号或密码不对，或者 token 过期了）")
        _set(phase="上传中", note="开始上传…")
        res = post_pack(base, tok, pack, dry_run)
        _set(running=False, phase="完成", finished=time.time(), result=res,
             note=("预演完成（服务器没写库）" if dry_run else "上传完成 ✓"))
        log.info("推送完成：%s", res)
        return res
    except Exception as exc:                                     # noqa: BLE001
        _set(running=False, phase="失败", finished=time.time(),
             error=f"{type(exc).__name__}: {exc}", note="出错了")
        log.warning("推送失败：%s", exc)
        raise
    finally:
        if pack is not None:
            try:
                pack.unlink(missing_ok=True)
            except OSError:
                pass


def start_async(cfg, store, folder_ids: list[int], dry_run: bool = True) -> bool:  # noqa: ANN001
    """后台跑（界面按钮点一下就走）✓ 已经在跑就返回 False"""
    with _LOCK:
        if _STATE["running"]:
            return False
    def _work() -> None:
        try:
            run(cfg, store, folder_ids, dry_run=dry_run)
        except Exception:                                        # noqa: BLE001
            pass
    threading.Thread(target=_work, name="eg2-syncpush", daemon=True).start()
    return True
