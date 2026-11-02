"""让"建库那台机器"响应服务器上的同步请求（多人 P4-g）。

用户要的是：**哪边点同步按钮都能同步** ✓（不是"服务器上不能同步" ✗）。
但同步脚本只在本机（它要 ssh 过去操作服务器 ✓），服务器自己跑不了 ✗。
所以这样接：

    服务器界面点「同步」 → 服务器记下请求（data/tmp/sync-request.json ✓）
    → **本机**每 20 秒问一次服务器"有没有新请求" ✓
    → 看到新的就立刻跑一次完整同步 ✓

于是一分钟内无论在哪边点，都会真的同步 ✓；服务器上的按钮不再"报错"也不
"装死" ✓，而是显示"已请求，本机正在执行" ✓。

只在本机启动这个轮询器（有 run/deploy/sync_all.py 才算"建库那台机器" ✓），
服务器上不启动 ✓。服务器地址默认 192.168.1.10:8199，可用环境变量
EG2_SERVER_URL 覆盖 ✓。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("eg2.syncpoll")

POLL_SECONDS = int(os.environ.get("EG2_SYNC_POLL", "15") or 15)
NOWIN = 0x08000000 if os.name == "nt" else 0


def start(cfg) -> bool:                                          # noqa: ANN001
    """在本机启动同步请求轮询器；不是"建库那台机器"就什么都不做。"""
    root = Path(cfg.root)
    script = root / "run" / "deploy" / "sync_all.py"
    if not script.is_file():
        return False                       # 服务器：没有脚本，不轮询 ✓
    server = str(os.environ.get("EG2_SERVER_URL", "http://192.168.1.10:8199")).rstrip("/")
    state_file = root / "run" / "deploy" / "_sync_request_state.json"

    def last_seen() -> float:
        try:
            return float(json.loads(state_file.read_text(encoding="utf-8")).get("ts") or 0)
        except (OSError, ValueError):
            return 0.0

    def remember(ts: float) -> None:
        try:
            state_file.write_text(json.dumps({"ts": ts}), encoding="utf-8")
        except OSError:
            pass

    def python_exe() -> str:
        for rel in ("venv/Scripts/python.exe", "venv/bin/python"):
            p = root / rel
            if p.is_file():
                return str(p)
        return os.sys.executable

    def loop() -> None:
        import urllib.error
        import urllib.request

        last = last_seen()
        idle_bad = 0
        while True:
            time.sleep(POLL_SECONDS)
            try:
                with urllib.request.urlopen(
                        server + "/api/admin/sync-status", timeout=15) as r:
                    st = json.loads(r.read().decode("utf-8", "replace"))
                idle_bad = 0
            except (urllib.error.URLError, OSError, ValueError) as exc:
                idle_bad += 1
                if idle_bad in (1, 10):                 # 别刷屏
                    log.info("问服务器同步请求失败（第 %d 次）：%s", idle_bad, exc)
                continue
            req = st.get("requested") or {}
            try:
                ts = float(req.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts <= last:
                continue
            last = ts
            remember(ts)
            who = str(req.get("by") or "?")
            log.info("服务器上有人点了同步（%s，%s）→ 现在执行", who,
                     time.strftime("%H:%M:%S", time.localtime(ts)))
            try:
                proc = subprocess.run([python_exe(), "-u", str(script)],
                                      cwd=str(root), creationflags=NOWIN,
                                      capture_output=True, text=True, timeout=3600)
                log.info("响应服务器请求的同步完成：退出码 %s", proc.returncode)
            except Exception as exc:                     # noqa: BLE001
                log.warning("响应服务器请求的同步失败：%s", exc)

    threading.Thread(target=loop, name="eg2-sync-poller", daemon=True).start()
    log.info("同步请求轮询器已启动（每 %d 秒问一次 %s）", POLL_SECONDS, server)
    return True
