"""界面上的「立即同步」按钮（多人 P4-c）。

为什么要它：定时同步是兜底（15 分钟），但"我刚在服务器上审批了账号 / 刚在本机
加了文件"这种场景需要**点一下就生效** ✓。

两条通道：
  · **运行同步**：只有"建库的那台机器"（本机）能真正执行 —— 同步脚本在本机，
    它 ssh 过去操作服务器。所以本机点按钮 = 立刻跑一次完整同步 ✓
  · **请求同步**：服务器上没有同步脚本 → 记一条"请求"，界面会如实告诉用户
    「请在建库的那台机器上点同步」（服务器侧的账号改动由本机的下次同步带走 ✓）

安全：只允许 ① 管理员登录会话 ② 本机(loopback) 调用；并且**60 秒内只跑一次**
（避免有人连点把机器压住）。同步在后台线程里跑，接口立刻返回，不阻塞界面。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .auth import COOKIE_NAME, Auth
from .config import Config

log = logging.getLogger("eg2.sync")
NOWIN = 0x08000000 if os.name == "nt" else 0
MIN_GAP = 60             # 两次执行的最小间隔（秒）


def create_router(cfg: Config) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["sync"])
    root = Path(cfg.root)
    script = root / "run" / "deploy" / "sync_all.py"
    logfile = root / "run" / "deploy" / "sync_all.log"
    req_file = Path(cfg.db_path).parent.parent / "tmp" / "sync-request.json"
    auth = Auth(cfg.db_path)
    state: dict = {"running": False, "started": 0.0, "by": "", "result": None,
                   "finished": 0.0}
    lock = threading.Lock()

    def python_exe() -> Path:
        for rel in ("venv/Scripts/python.exe", "venv/bin/python"):
            p = root / rel
            if p.is_file():
                return p
        return Path(os.sys.executable)

    def can_run_here() -> bool:
        return script.is_file()

    def _run(by: str) -> None:
        try:
            proc = subprocess.run([str(python_exe()), "-u", str(script)],
                                  cwd=str(root), creationflags=NOWIN,
                                  capture_output=True, text=True, timeout=3600)
            with lock:
                state["result"] = (proc.returncode, (proc.stdout or "")[-400:])
        except Exception as exc:                                 # noqa: BLE001
            with lock:
                state["result"] = (-1, f"启动失败：{exc}")
        finally:
            with lock:
                state["running"] = False
                state["finished"] = time.time()
                state["by"] = by
            log.info("手动同步结束：%s", state.get("result"))

    def _tail(n: int = 8) -> list[str]:
        try:
            return logfile.read_text(encoding="utf-8", errors="replace") \
                .strip().splitlines()[-n:]
        except OSError:
            return []

    def _check(request: Request) -> str:
        """返回调用者标识；不合法就抛 403。"""
        host = request.client.host if request.client else ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return f"本机({host})"
        u = auth.who(request.cookies.get(COOKIE_NAME))
        if not u:
            raise HTTPException(status_code=403, detail="请先登录（或在本机操作）")
        if u.get("role") != "admin":
            raise HTTPException(status_code=403, detail="只有管理员能触发同步")
        return str(u.get("name") or "?")

    @router.post("/sync-now")
    async def sync_now(request: Request):
        who = _check(request)
        with lock:
            if state["running"]:
                return {"ok": True, "running": True, "note": "正在同步，请稍候"}
            gap = time.time() - float(state["finished"] or 0)
            if state["finished"] and gap < MIN_GAP:
                return {"ok": True, "running": False,
                        "note": f"刚刚同步过（{int(gap)} 秒前），{MIN_GAP - int(gap)} 秒后可再点"}
            if not can_run_here():
                # 服务器侧：记一条请求，界面如实提示
                try:
                    req_file.parent.mkdir(parents=True, exist_ok=True)
                    req_file.write_text(json.dumps(
                        {"ts": time.time(), "by": who}, ensure_ascii=False),
                        encoding="utf-8")
                except OSError as exc:
                    raise HTTPException(status_code=500,
                                        detail=f"写请求标记失败：{exc}") from exc
                return {"ok": True, "running": False, "requested": True,
                        "note": "这台是服务器：同步要在建库的那台机器上执行"
                                "（已记下请求）"}
            state.update(running=True, started=time.time(), result=None)
            threading.Thread(target=_run, args=(who,), daemon=True).start()
        log.info("手动同步已触发，by=%s", who)
        return {"ok": True, "running": True, "note": "已开始同步"}

    @router.get("/sync-status")
    async def sync_status():
        with lock:
            running = bool(state["running"])
            started = float(state["started"] or 0)
            res = state["result"]
        requested = None
        try:
            if req_file.is_file():
                requested = json.loads(req_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            requested = None
        return {"ok": True, "running": running, "started": started,
                "can_run_here": can_run_here(),
                "elapsed": round(time.time() - started, 1) if running and started else 0,
                "result_code": (res[0] if res else None),
                "result_tail": (res[1] if res else ""),
                "requested": requested,
                "tail": _tail()}

    def _local_folders() -> list[str]:
        """本机索引的文件夹（就是需要同步的那些）。"""
        try:
            con = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
            rows = [str(r[0]) for r in con.execute("SELECT path FROM folders")]
            con.close()
            return rows
        except sqlite3.Error:
            return []

    _walk_cache: dict = {"at": 0.0, "res": (False, "")}

    def _file_dirty(last: float) -> tuple[bool, str]:
        """有没有比"上次同步"更新的文件。

        **只查本机硬盘**（跳过 \\\\ 开头的 UNC/网络盘）：NAS 上三万多文件，
        每 20 秒走一遍太贵。结果缓存 30 秒，避免反复扫。
        """
        if time.time() - _walk_cache["at"] < 30:
            return _walk_cache["res"]
        res = (False, "")
        for root_dir in _local_folders():
            if root_dir.startswith("\\\\") or not os.path.isdir(root_dir):
                continue
            try:
                for dirpath, dirnames, filenames in os.walk(root_dir):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for fn in filenames:
                        try:
                            if os.path.getmtime(os.path.join(dirpath, fn)) > last:
                                res = (True, f"{Path(root_dir).name} 里有新改动")
                                break
                        except OSError:
                            continue
                    if res[0]:
                        break
            except OSError:
                continue
            if res[0]:
                break
        _walk_cache.update(at=time.time(), res=res)
        return res

    @router.get("/sync-check")
    async def sync_check():
        """有没有"还没同步"的改动 —— 给按钮上色用。

        判据：
          ① 本机硬盘上的知识库文件夹里，有文件比"上次同步"更新 → 有改动 ✓
          ② 索引库文件自上次成功同步以来被改过（重建索引/改配置）→ 有改动 ✓
          ③ 上次同步是失败的 → 有改动（提醒你再点一次）✓
        **不是本机**（服务器上没有同步脚本）→ 一律不报"有改动"，
        否则服务器界面会永远显示红色（误报，实测踩过）。
        """
        if not can_run_here():
            return {"ok": True, "dirty": False, "reasons": [],
                    "can_run_here": False,
                    "note": "这台是服务器：同步要在建库的那台机器上执行"}

        state_file = root / "run" / "deploy" / "_db_sync_state.json"
        prev: dict = {}
        try:
            prev = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        reasons: list[str] = []

        # 上次同步时间：优先用状态文件里记的，退回日志文件的修改时间
        last = 0.0
        try:
            last = float(prev.get("ts") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if not last:
            try:
                last = (root / "run" / "deploy" / "sync_all.log").stat().st_mtime
            except OSError:
                last = 0.0
        if last:
            dirty_f, why = _file_dirty(last)
            if dirty_f:
                reasons.append(why)

        dbp = Path(cfg.db_path)
        try:
            sig = prev.get("sig") or {}
            old = sig.get("db")
            now = [dbp.stat().st_size, round(dbp.stat().st_mtime, 1)]
            if old and list(old) != list(now):
                reasons.append("索引库有改动")
        except OSError:
            reasons.append("索引库读不到")
        with lock:
            res = state["result"]
            finished = float(state["finished"] or 0)
        if res and res[0] not in (0, None):
            reasons.append("上次同步失败")
        return {"ok": True, "dirty": bool(reasons), "reasons": reasons,
                "last_sync": (prev.get("at") or ""),
                "can_run_here": True,
                "just_synced": bool(finished and (time.time() - finished) < 60
                                    and res and res[0] == 0)}

    return router
