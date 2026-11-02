"""账号、会话、权限 —— 多人使用的 P1 地基。

零新依赖：PBKDF2 用标准库 hashlib，令牌用 secrets，存储复用主索引库（同一个
SQLite 文件，省得再引一个库、也天然跟着现有的 DB 同步走）。

设计要点（详见 docs/多人使用设计.md）：
  · 密码只存 `salt$pbkdf2_sha256`，20 万轮；比对用 hmac.compare_digest（抗时序）
  · 会话令牌 32 字节随机，存 sessions 表；Cookie 是 HttpOnly + SameSite=Lax
  · CSRF：登录时下发 csrf（= 令牌前 16 位），所有写操作要带 X-EG2-CSRF 头；
    跨站表单带不上自定义头，所以这层够用（HTTPS 另议）
  · 登录限速：同 IP+用户名 5 分钟内失败 5 次锁 5 分钟
  · **首启放行**：users 表为空时，系统处于"未初始化"状态，只允许创建管理员 ——
    这样老用户升级上来不会被自己锁在外面
  · 本阶段只**建立能力**（表 + 接口），还不强制拦截既有接口；
    拦截与界面在 P2/P3 一起上，保证任何阶段项目都能正常用

用法（进程内）：
    from .auth import Auth
    auth = Auth(cfg.db_path)
    auth.init()                       # 建表（幂等）
    auth.need_bootstrap()             # 还没有任何用户 → 该显示"创建管理员"
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("eg2.auth")

PBKDF2_ROUNDS = 200_000
SESSION_SECONDS = 7 * 24 * 3600
COOKIE_NAME = "eg2_sid"
CSRF_HEADER = "X-EG2-CSRF"
LOGIN_WINDOW = 300           # 限速窗口（秒）
LOGIN_MAX_FAIL = 5           # 窗口内允许的失败次数
ROLES = ("admin", "user")
STATES = ("pending", "active", "disabled")

_fails: dict[str, list[float]] = {}      # 登录失败记录（进程内即可，重启清零）


def _hash_pwd(pwd: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"),
                             PBKDF2_ROUNDS)
    return f"{salt}${dk.hex()}"


def _check_pwd(pwd: str, stored: str) -> bool:
    try:
        salt, _hex = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(_hash_pwd(pwd, salt), stored)


class Auth:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    # ---------------- 底层 ----------------
    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=20)
        con.row_factory = sqlite3.Row
        return con

    def init(self) -> None:
        """建表（幂等）。加列式的迁移留给 P2，这里只建新表。"""
        con = self._con()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    pwd TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    last_login REAL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    csrf TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    ip TEXT, ua TEXT
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL, user_id INTEGER, action TEXT, detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                """
            )
            con.commit()
        finally:
            con.close()

    def audit(self, action: str, user_id: int | None = None, detail: str = "") -> None:
        try:
            con = self._con()
            with con:
                con.execute("INSERT INTO audit(ts,user_id,action,detail) VALUES(?,?,?,?)",
                            (time.time(), user_id, action, detail[:400]))
            con.close()
        except sqlite3.Error as exc:                              # noqa: BLE001
            log.warning("审计写入失败（不影响主流程）：%s", exc)

    # ---------------- 用户 ----------------
    def count(self) -> int:
        con = self._con()
        try:
            return int(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        finally:
            con.close()

    def need_bootstrap(self) -> bool:
        return self.count() == 0

    def get_by_name(self, name: str) -> dict | None:
        con = self._con()
        try:
            row = con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def get(self, uid: int) -> dict | None:
        con = self._con()
        try:
            row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def list_users(self) -> list[dict]:
        con = self._con()
        try:
            rows = con.execute(
                "SELECT id,name,role,state,created_at,last_login FROM users "
                "ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def create_user(self, name: str, pwd: str, role: str = "user",
                    state: str = "active") -> dict:
        name = (name or "").strip()
        if not (2 <= len(name) <= 24):
            raise ValueError("用户名要 2~24 个字符")
        if len(pwd or "") < 6:
            raise ValueError("密码至少 6 位")
        if role not in ROLES:
            raise ValueError("角色只能是 admin / user")
        if state not in STATES:
            raise ValueError("状态不合法")
        con = self._con()
        try:
            with con:
                cur = con.execute(
                    "INSERT INTO users(name,pwd,role,state,created_at) VALUES(?,?,?,?,?)",
                    (name, _hash_pwd(pwd), role, state, time.time()))
            uid = int(cur.lastrowid)
            self.audit("create_user", uid, f"{name} role={role} state={state}")
            return self.get(uid) or {}
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"用户名「{name}」已经被用了") from exc
        finally:
            con.close()

    def set_state(self, uid: int, state: str) -> None:
        if state not in STATES:
            raise ValueError("状态不合法")
        con = self._con()
        try:
            with con:
                con.execute("UPDATE users SET state=? WHERE id=?", (state, uid))
            self.audit("set_state", uid, state)
        finally:
            con.close()

    def set_role(self, uid: int, role: str) -> None:
        if role not in ROLES:
            raise ValueError("角色不合法")
        con = self._con()
        try:
            with con:
                con.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
            self.audit("set_role", uid, role)
        finally:
            con.close()

    def reset_password(self, uid: int, pwd: str) -> None:
        if len(pwd or "") < 6:
            raise ValueError("密码至少 6 位")
        con = self._con()
        try:
            with con:
                con.execute("UPDATE users SET pwd=? WHERE id=?", (_hash_pwd(pwd), uid))
            self.audit("reset_password", uid)
        finally:
            con.close()

    def verify_pwd(self, uid: int, pwd: str) -> bool:
        """这个密码对不对（改密码 / 改名前的身份确认）。"""
        con = self._con()
        try:
            row = con.execute("SELECT pwd FROM users WHERE id=?",
                              (int(uid),)).fetchone()
        finally:
            con.close()
        # ⚠ 密码是 PBKDF2 + 随机 salt，必须用 _check_pwd 比（直接比哈希永远不等 ✗）
        return bool(row) and _check_pwd(pwd or "", str(row[0]))

    def rename(self, uid: int, name: str) -> dict:
        """改用户名（检查长度 + 唯一）。"""
        name = (name or "").strip()
        if not (2 <= len(name) <= 24):
            raise ValueError("用户名要 2~24 个字符")
        con = self._con()
        try:
            with con:
                dup = con.execute(
                    "SELECT id FROM users WHERE name=? AND id<>?",
                    (name, int(uid))).fetchone()
                if dup:
                    raise ValueError("这个用户名已经被占用了")
                con.execute("UPDATE users SET name=?, updated_at=? WHERE id=?",
                            (name, time.time(), int(uid)))
            self.audit("rename_self", int(uid), name)
        finally:
            con.close()
        return self.get(uid) or {}

    def keep_only_session(self, uid: int, token: str | None) -> int:
        """改密码后清掉该用户的**其它**会话（留着当前这个，省得把自己登出）。

        返回被清掉的条数。token 为空时清掉全部。
        """
        con = self._con()
        try:
            with con:
                if token:
                    cur = con.execute(
                        "DELETE FROM sessions WHERE user_id=? AND token<>?",
                        (int(uid), token))
                else:
                    cur = con.execute(
                        "DELETE FROM sessions WHERE user_id=?", (int(uid),))
                n = cur.rowcount or 0
            self.audit("kick_sessions", int(uid), f"{n} 个")
        finally:
            con.close()
        return int(n)

    # ---------------- 登录 / 会话 ----------------
    def _rate_key(self, ip: str, name: str) -> str:
        return f"{ip}|{name}"

    def locked(self, ip: str, name: str) -> int:
        """返回还要锁多少秒（0 = 没锁）。"""
        rec = _fails.get(self._rate_key(ip, name)) or []
        now = time.time()
        rec = [t for t in rec if now - t < LOGIN_WINDOW]
        _fails[self._rate_key(ip, name)] = rec
        if len(rec) < LOGIN_MAX_FAIL:
            return 0
        return int(LOGIN_WINDOW - (now - rec[0])) + 1

    def login(self, name: str, pwd: str, ip: str = "", ua: str = "") -> dict:
        left = self.locked(ip, name)
        if left:
            raise PermissionError(f"失败次数太多，请 {left} 秒后再试")
        u = self.get_by_name(name)
        if not u or not _check_pwd(pwd, u["pwd"]):
            _fails.setdefault(self._rate_key(ip, name), []).append(time.time())
            self.audit("login_fail", u["id"] if u else None, f"{name}@{ip}")
            raise PermissionError("用户名或密码不对")
        if u["state"] == "pending":
            raise PermissionError("账号还在等管理员审批")
        if u["state"] != "active":
            raise PermissionError("账号已被停用")
        _fails.pop(self._rate_key(ip, name), None)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_hex(8)
        now = time.time()
        con = self._con()
        try:
            with con:
                con.execute("INSERT INTO sessions(token,user_id,csrf,created_at,"
                            "expires_at,ip,ua) VALUES(?,?,?,?,?,?,?)",
                            (token, u["id"], csrf, now, now + SESSION_SECONDS, ip, ua[:200]))
                con.execute("UPDATE users SET last_login=? WHERE id=?", (now, u["id"]))
                con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self.audit("login", u["id"], f"{name}@{ip}")
        finally:
            con.close()
        return {"token": token, "csrf": csrf, "user": self.public(u)}

    def logout(self, token: str) -> None:
        con = self._con()
        try:
            with con:
                con.execute("DELETE FROM sessions WHERE token=?", (token,))
        finally:
            con.close()

    def who(self, token: str | None) -> dict | None:
        """令牌 → 用户（顺带返回 csrf）。过期/无效返回 None。"""
        if not token:
            return None
        con = self._con()
        try:
            row = con.execute(
                "SELECT s.token, s.csrf, s.expires_at, u.* FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token=?", (token,)).fetchone()
            if not row:
                return None
            d = dict(row)
            if float(d.get("expires_at") or 0) < time.time():
                return None
            if d.get("state") != "active":
                return None
            return d
        finally:
            con.close()

    @staticmethod
    def public(u: dict) -> dict:
        """给前端看的用户信息（绝不带密码/令牌）。"""
        return {"id": u.get("id"), "name": u.get("name"), "role": u.get("role"),
                "state": u.get("state")}

    @staticmethod
    def check_csrf(user: dict, header: str | None) -> bool:
        return bool(user) and bool(header) and hmac.compare_digest(
            str(user.get("csrf") or ""), str(header))
