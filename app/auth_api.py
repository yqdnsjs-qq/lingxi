"""账号与会话的 HTTP 接口（多人使用 P1）。

前缀 /api/auth：
    GET  /status          当前登录状态 / 是否需要"创建管理员"
    POST /bootstrap       首次创建管理员（仅当还没有任何用户时可用）
    POST /register        自助注册（进入 pending，等管理员审批）
    POST /login           登录（下发 HttpOnly 会话 Cookie + csrf）
    POST /logout          退出
    GET  /me              我是谁
    GET  /users           用户列表（管理员）
    POST /users/{id}/state      启用/停用/待审批（管理员，需 CSRF）
    POST /users/{id}/role       改角色（管理员，需 CSRF）
    POST /users/{id}/password   重置密码（管理员，需 CSRF）

注意：本阶段**不拦截**既有业务接口（那在 P2/P3 和登录界面一起上），
所以加了这一层之后项目仍然照常可用。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Request, Response

from .auth import COOKIE_NAME, CSRF_HEADER, SESSION_SECONDS, Auth
from .config import Config

log = logging.getLogger("eg2.auth")


def create_router(cfg: Config) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])
    auth = Auth(cfg.db_path)
    auth.init()

    # ---------- 小工具 ----------
    def _ip(request: Request) -> str:
        return (request.client.host if request.client else "") or ""

    def _ua(request: Request) -> str:
        return request.headers.get("user-agent", "")

    def _cur(request: Request) -> dict:
        u = auth.who(request.cookies.get(COOKIE_NAME))
        if not u:
            raise HTTPException(status_code=401, detail="请先登录")
        return u

    def _admin(request: Request, csrf: bool = True) -> dict:
        u = _cur(request)
        if u.get("role") != "admin":
            raise HTTPException(status_code=403, detail="只有管理员能做这个")
        if csrf and not Auth.check_csrf(u, request.headers.get(CSRF_HEADER)):
            raise HTTPException(status_code=403,
                                detail=f"缺少或错误的 {CSRF_HEADER} 头")
        return u

    def _login_out(response: Response, name: str, pwd: str, request: Request) -> dict:
        try:
            d = auth.login(name, pwd, _ip(request), _ua(request))
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        # ★ 2026-10-21 外网部署：外网走 Cloudflare（HTTPS），内网还是 HTTP。
        #   secure 写死会坏一头：写 True → 内网登录不上；写 False → 外网明文传。
        #   所以按**这次请求的协议**决定（信任 X-Forwarded-Proto）✓
        _is_https = False
        try:
            _is_https = (request.url.scheme == "https"
                         or (request.headers.get("x-forwarded-proto") or "")
                         .lower().startswith("https"))
        except Exception:                                        # noqa: BLE001
            _is_https = False
        response.set_cookie(COOKIE_NAME, d["token"], max_age=SESSION_SECONDS,
                            httponly=True, samesite="lax", path="/",
                            secure=_is_https)
        return {"ok": True, "user": d["user"], "csrf": d["csrf"]}

    def _make(name: str, pwd: str, role: str, state: str) -> dict:
        try:
            return auth.create_user(name, pwd, role=role, state=state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---------- 注册开关（运行时可变，存 meta，改完立即生效）----------
    def _allow_register() -> bool:
        """读注册开关：meta 里有就用它，否则用 config.ini 的默认值。"""
        try:
            con = auth._con()                                    # noqa: SLF001
            try:
                row = con.execute(
                    "SELECT value FROM meta WHERE key='auth.allow_register'"
                ).fetchone()
            finally:
                con.close()
            if row is not None:
                return str(row[0]).strip() in ("1", "yes", "true", "on")
        except Exception:                                        # noqa: BLE001
            pass
        return bool(cfg.allow_register)

    def _set_allow_register(on: bool) -> None:
        con = auth._con()                                        # noqa: SLF001
        try:
            with con:
                con.execute(
                    "INSERT INTO meta(key,value) VALUES('auth.allow_register',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("1" if on else "0",))
        finally:
            con.close()

    # ---------- 接口 ----------
    @router.get("/status")
    async def status(request: Request):
        u = auth.who(request.cookies.get(COOKIE_NAME))
        # ★ 2026-10-21 外网：账号数量**只在本机**返回（外网不该暴露"有几个账号"）
        _local = False
        try:
            from .filesearch import _can_index_here

            _local = bool(_can_index_here())
        except Exception:                                        # noqa: BLE001
            _local = False
        return {"ok": True,
                "allow_register": _allow_register(),
                "need_bootstrap": auth.need_bootstrap(),
                "users": (auth.count() if _local else None),
                # csrf 只回给"会话本人"（别人拿不到），管理页要用它做写操作
                "csrf": (u or {}).get("csrf", "") if u else "",
                "user": Auth.public(u) if u else None}

    @router.post("/bootstrap")
    async def bootstrap(request: Request, response: Response,
                        payload: dict = Body(default={})):
        if not auth.need_bootstrap():
            raise HTTPException(status_code=400, detail="已经有管理员了，请直接登录")
        name = str(payload.get("name") or "").strip()
        pwd = str(payload.get("pwd") or "")
        _make(name, pwd, "admin", "active")
        return _login_out(response, name, pwd, request)

    @router.post("/register")
    async def register(request: Request, payload: dict = Body(default={})):
        if auth.need_bootstrap():
            raise HTTPException(status_code=400,
                                detail="系统还没初始化：请先创建管理员")
        # ★ 2026-10-21 外网部署：注册默认**关闭**，要开得在 config.ini 里写
        #   [multi] allow_register = yes（注册出来的账号仍是 pending，等审批）
        if not _allow_register():
            raise HTTPException(
                status_code=403,
                detail="本服务未开放自助注册：请联系管理员开通账号 ✓")
        # 限流：同 IP 1 小时最多 3 次，防止有人灌垃圾账号
        from . import ratelimit as _rl

        ok, wait = _rl.hit("register", _rl.client_ip(request))
        if not ok:
            raise HTTPException(
                status_code=429,
                detail=f"注册太频繁了，请 {wait} 秒后再试")
        name = str(payload.get("name") or "").strip()
        pwd = str(payload.get("pwd") or "")
        u = _make(name, pwd, "user", "pending")
        return {"ok": True, "user": Auth.public(u),
                "note": "已提交，等管理员审批后即可登录"}

    @router.post("/login")
    async def login(request: Request, response: Response,
                    payload: dict = Body(default={})):
        # 撞库防护：同 IP 5 分钟内最多 10 次；成功登录后清计数
        from . import ratelimit as _rl

        _ip = _rl.client_ip(request)
        _ok, _wait = _rl.hit("login", _ip)
        if not _ok:
            raise HTTPException(
                status_code=429,
                detail=f"登录尝试太频繁，请 {_wait} 秒后再试")
        return _login_out(response, str(payload.get("name") or "").strip(),
                          str(payload.get("pwd") or ""), request)

    @router.post("/logout")
    async def logout(request: Request, response: Response):
        tok = request.cookies.get(COOKIE_NAME)
        if tok:
            auth.logout(tok)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @router.get("/me")
    async def me(request: Request):
        return {"ok": True, "user": Auth.public(_cur(request))}

    @router.post("/me/password")
    async def my_password(request: Request, payload: dict = Body(default={})):
        """改**自己**的密码：要先给旧密码 ✓"""
        u = _cur(request)
        old_pwd = str(payload.get("old") or "")
        new_pwd = str(payload.get("new") or "")
        if len(new_pwd) < 6:
            raise HTTPException(status_code=400, detail="新密码至少 6 位")
        if not auth.verify_pwd(int(u["id"]), old_pwd):
            raise HTTPException(status_code=400, detail="旧密码不对")
        if old_pwd == new_pwd:
            raise HTTPException(status_code=400, detail="新密码和旧的一样")
        auth.reset_password(int(u["id"]), new_pwd)
        # 改完密码把**其它**设备上的会话清掉（当前这个留着）
        n = 0
        try:
            n = auth.keep_only_session(int(u["id"]),
                                       request.cookies.get(COOKIE_NAME))
        except Exception:                                        # noqa: BLE001
            pass
        log.info("用户自己改密码：%s（清掉 %d 个其它会话）", u.get("name"), n)
        return {"ok": True,
                "note": f"密码已更新（其它设备上的 {n} 个登录已失效）"}

    @router.post("/me/name")
    async def my_name(request: Request, payload: dict = Body(default={})):
        """改**自己**的用户名：要验一次密码 ✓"""
        u = _cur(request)
        name = str(payload.get("name") or "").strip()
        pwd = str(payload.get("pwd") or "")
        if not auth.verify_pwd(int(u["id"]), pwd):
            raise HTTPException(status_code=400, detail="密码不对")
        if name == u.get("name"):
            return {"ok": True, "user": Auth.public(u), "note": "没改（一样）"}
        try:
            nu = auth.rename(int(u["id"]), name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("用户自己改名：%s → %s", u.get("name"), name)
        return {"ok": True, "user": Auth.public(nu)}

    @router.get("/users")
    async def users(request: Request):
        _admin(request, csrf=False)
        return {"ok": True, "users": auth.list_users()}

    @router.get("/settings")
    async def get_settings(request: Request):
        """管理员：读注册开关（只读，不需要 CSRF）。"""
        _admin(request, csrf=False)
        return {"ok": True, "allow_register": _allow_register()}

    @router.post("/settings")
    async def set_settings(request: Request, payload: dict = Body(default={})):
        """管理员：切注册开关（立即生效，不用重启）。"""
        _admin(request)
        if "allow_register" not in payload:
            raise HTTPException(status_code=400, detail="缺少 allow_register")
        on = bool(payload.get("allow_register"))
        _set_allow_register(on)
        log.info("注册开关 → %s（管理员 %s）", "开" if on else "关",
                 (_cur(request) or {}).get("name", ""))
        return {"ok": True, "allow_register": on,
                "note": "已生效：登录页的注册入口会跟着变"}

    @router.post("/users")
    async def create_user(request: Request, payload: dict = Body(default={})):
        """管理员：直接新建一个用户（默认就是 active，不用再审批）。"""
        _admin(request)
        name = str(payload.get("name") or "").strip()
        pwd = str(payload.get("pwd") or "")
        role = str(payload.get("role") or "user").strip()
        state = str(payload.get("state") or "active").strip()
        if state not in ("active", "pending", "disabled"):
            raise HTTPException(status_code=400, detail="状态不合法")
        try:
            u = auth.create_user(name, pwd, role=role, state=state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("管理员新建用户：%s（%s / %s）",
                 name, role, state)
        return {"ok": True, "user": Auth.public(u)}

    @router.post("/users/{uid}/state")
    async def set_state(uid: int, request: Request, payload: dict = Body(default={})):
        me_u = _admin(request)
        if uid == me_u["id"] and str(payload.get("state")) != "active":
            raise HTTPException(status_code=400, detail="不能停用/降级自己")
        try:
            auth.set_state(uid, str(payload.get("state") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "users": auth.list_users()}

    @router.post("/users/{uid}/role")
    async def set_role(uid: int, request: Request, payload: dict = Body(default={})):
        me_u = _admin(request)
        if uid == me_u["id"] and str(payload.get("role")) != "admin":
            raise HTTPException(status_code=400, detail="不能把自己降成普通用户")
        try:
            auth.set_role(uid, str(payload.get("role") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "users": auth.list_users()}

    @router.post("/users/{uid}/password")
    async def set_pwd(uid: int, request: Request, payload: dict = Body(default={})):
        _admin(request)
        try:
            auth.reset_password(uid, str(payload.get("pwd") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    return router
