"""登录守卫（多人 P2-b）：按需要给业务接口加"必须登录"。

**默认关闭**，用环境变量打开：

    EG2_ENFORCE_LOGIN=1     打开（未登录访问业务接口 → 401）

为什么要做成"开关 + 白名单"：
  开 401 会连带挡住**机器对机器**的调用 —— 每 15 分钟的同步任务（ssh 调
  /api/admin/reload-db）、Dify 外部知识库、验收脚本，它们都不带 Cookie ✗。
  一刀切会把系统搞坏，而且不容易发现（同步静默失败）。所以：
    · 地面规则：**默认关闭**，显式打开才生效（升级不会突然把人锁在外面）
    · 白名单：健康检查、静态资源、登录接口、Dify、本机管理接口一律放行
    · 放行判定**失败也放行**（取不到用户就当没登录 → 401；但白名单判断出错时放行，
      宁可漏挡也不要把人挡在门外）

白名单里的路径（前缀匹配）：
    /health  /static/  /search  /api/auth/  /dify/  /api/admin/

**只给本机的白名单**（第四件事，2026-10-16 加）：
    /api/ui-prefs  —— 推库脚本 `run/deploy/sync_db.py` 用它 dump/回灌界面偏好，
    但它**不在**前缀白名单里 ✗：一旦开强制登录，同步会被 401 挡死（而且静默失败）。
    直接放进白名单也不合适 ✗ —— 里面存着外部模型的 api_key，无异于把密钥公开。
    所以规则是**按来源 IP**：只有 `127.0.0.1 / ::1`（脚本就在本机跑）放行 ✓，
    局域网里的其他人照样要登录 ✓。转发/反代后面所有人都像本机，本项目不用反代 ✓。
"""
from __future__ import annotations

import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import COOKIE_NAME, Auth

log = logging.getLogger("eg2.authguard")

ALLOW_PREFIX = (
    "/health", "/static/", "/search", "/api/auth/", "/dify/", "/api/admin/",
    "/favicon",
)
# 只读的静态/文档类也放行
# `/` 放行：server.py 会按机器分流（本机→嵌入调试页，服务器→302 到搜索页），
# 跳过去之后如果没登录，再由守卫带去登录页 ✓
ALLOW_EXACT = ("/", "/docs", "/openapi.json", "/redoc")

# 这几个**本机专用**的接口也要放行（它们自己会校验来源 IP ✓）：
#   /admin/shutdown —— stop.bat 和 run\restart_server.py 靠它让服务**优雅退出**
#     （落盘索引状态、释放显存）。2026-10-17 发现它被强制登录挡了 401 ✗ ——
#     表现是"优雅退出超时，只能强杀" ✗（强杀有丢状态的风险）。
#     放宽是安全的：端点内部只允许 127.0.0.1 ✓，局域网访问照样 403 ✓。
ALLOW_LOCAL_EXTRA = ("/admin/shutdown", "/api/ui-leaving", "/api/ui-alive")

# 本机专用白名单（同步脚本要用，但不能给局域网）
#   /api/ui-prefs  —— 推库脚本 dump/回灌界面偏好（里面有外部模型 api_key ✗ 不能公开）
#   /api/settings  —— 推库脚本拿它当"设置页金丝雀"（判断热重载后设置页还能读，
#                     这正是当年"只 rebind 一处 → 设置页 500"那个 bug 的检测点）。
#                     它只能从**服务器自己的 127.0.0.1** 调（ssh curl）✓，
#                     因为换库窗口里服务器的会话表已被本机那份覆盖，
#                     带会话反而必然 401 ✗（实测踩过）。
ALLOW_LOCAL = ("/api/ui-prefs", "/api/settings", *ALLOW_LOCAL_EXTRA)
LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost", "testclient")


def is_local(request) -> bool:                                # noqa: ANN001
    """请求是不是从本机发出来的（同步脚本走 127.0.0.1 ✓）。"""
    try:
        host = (request.client.host if request.client else "") or ""
        return host in LOCAL_HOSTS
    except Exception:                                         # noqa: BLE001
        return False


def enabled() -> bool:
    return str(os.environ.get("EG2_ENFORCE_LOGIN", "")).strip().lower() in (
        "1", "yes", "true", "on")


def _allowed(path: str) -> bool:
    if path in ALLOW_EXACT:
        return True
    return any(path.startswith(p) for p in ALLOW_PREFIX)


class LoginGuard(BaseHTTPMiddleware):
    """未登录访问业务接口 → 401（默认不启用，见 enabled()）。"""

    def __init__(self, app, db_path) -> None:                # noqa: ANN001
        super().__init__(app)
        self.auth = Auth(db_path)

    async def dispatch(self, request: Request, call_next):    # noqa: ANN001
        # **守卫自己**先看开关：即使有人无条件把它挂上来，没开开关也绝不拦人。
        # （只靠"挂载处判断"是不够的 —— 那种约定容易在后续改动里被绕过。）
        if not enabled():
            return await call_next(request)
        try:
            path = request.url.path
            if not _allowed(path):
                # 本机专用白名单（/api/ui-prefs）：同步脚本不带 Cookie，但它只在本机跑 ✓
                if any(path.startswith(p) for p in ALLOW_LOCAL) and is_local(request):
                    return await call_next(request)
                user = self.auth.who(request.cookies.get(COOKIE_NAME))
                if not user:
                    # ★ 2026-10-21：浏览器要页面时**跳登录页**，别返回 JSON ✗
                    #   （否则外网打开搜索页只会看到一坨 {"detail":...}）
                    _accept = (request.headers.get("accept") or "").lower()
                    if "text/html" in _accept:
                        from fastapi.responses import RedirectResponse

                        nxt = path if path.startswith("/") else "/"
                        return RedirectResponse(
                            "/static/login.html?next=" + nxt, status_code=302)
                    # 接口调用：401 + need_login，前端据此跳转 ✓
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "请先登录", "need_login": True},
                    )
        except Exception as exc:                              # noqa: BLE001
            # 守卫本身出错时**放行** —— 宁可漏挡，也不能因为一个 bug 把所有人挡在门外
            log.warning("登录守卫异常，放行本次请求：%s", exc)
        return await call_next(request)
