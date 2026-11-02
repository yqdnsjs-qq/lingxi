"""极简限流：内存滑动窗口（够用就好，不引第三方）。

为什么需要：服务一旦暴露到公网，`/api/auth/login` 和 `/api/auth/register`
就是最容易被撞库/灌垃圾的入口。没有限流的话：
  · 有人狂刷注册 → users 表塞满垃圾（虽然进不来，但管理员列表会被淹）
  · 有人撞密码 → 没有任何阻碍

设计：**按 (动作, 来源 IP) 记时间戳**，窗口内超过上限就拒绝。
进程重启就清空（可接受：这是防滥用，不是审计）。
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_HITS: dict[tuple[str, str], list[float]] = {}

# 各动作的默认额度：(窗口秒数, 窗口内最多几次)
RULES: dict[str, tuple[int, int]] = {
    "login": (300, 10),        # 5 分钟内最多 10 次登录尝试
    "register": (3600, 3),     # 1 小时最多注册 3 个号
    "chat": (60, 30),          # 1 分钟最多 30 条消息
    "upload": (3600, 40),      # 1 小时最多传 40 个附件
}


def _key(action: str, who: str) -> tuple[str, str]:
    return (action, who or "-")


def hit(action: str, who: str, limit: int | None = None,
        window: int | None = None) -> tuple[bool, int]:
    """记一次。返回 (是否允许, 还要等几秒)。

    `who` 一般是来源 IP；调用方自己保证已经取到真实 IP。
    """
    win, cap = RULES.get(action, (60, 60))
    if window is not None:
        win = window
    if limit is not None:
        cap = limit
    now = time.time()
    k = _key(action, who)
    with _LOCK:
        arr = [t for t in _HITS.get(k, []) if now - t < win]
        if len(arr) >= cap:
            wait = int(win - (now - arr[0])) + 1
            _HITS[k] = arr
            return False, max(1, wait)
        arr.append(now)
        _HITS[k] = arr
        return True, 0


def reset(action: str, who: str) -> None:
    """成功之后清掉计数（比如登录成功，就不该继续被限）。"""
    with _LOCK:
        _HITS.pop(_key(action, who), None)


def client_ip(request) -> str:                                   # noqa: ANN001
    """取真实来源 IP。走反向代理时要看 X-Forwarded-For 的第一个。"""
    try:
        # Cloudflare 会带 CF-Connecting-IP（最准），优先用它
        cf = request.headers.get("cf-connecting-ip") or ""
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for") or ""
        if xff:
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip") or ""
        if real:
            return real.strip()
        return request.client.host if request.client else "-"
    except Exception:                                            # noqa: BLE001
        return "-"


def cleanup(max_age: int = 7200) -> int:
    """清掉太久没动静的键（定时调用，避免内存一直长）。"""
    now = time.time()
    n = 0
    with _LOCK:
        for k in list(_HITS):
            arr = [t for t in _HITS[k] if now - t < max_age]
            if arr:
                _HITS[k] = arr
            else:
                del _HITS[k]
                n += 1
    return n


def stats() -> dict:
    with _LOCK:
        return {"keys": len(_HITS),
                "total": sum(len(v) for v in _HITS.values())}
