"""Dify 知识库的「归属 + 共享」规则（2026-10-22 项目主要求）。

背景：每个用户的 Dify 库存在自己的界面偏好里（`user_prefs.eg2-difylist`），
默认**只有本人能用**（项目主："不应该是个人连接个人的吗？"）。现在加共享：

每条库多两个字段：
    share      : "private"（默认，只有我）/ "all"（所有登录用户）/ "to"（名单）
    share_to   : ["张三", ...]   ← 只在 share == "to" 时有意义（存**用户名**）
    enabled    : false 表示这条先不用（本人和共享方都跳过）

为什么存名字而不是用户 id：账号可以改名（`/api/auth/me/name`），
名字在库里是 UNIQUE 的；这套系统就几个人，用名字更好读、也能手改 ✓。
真正的鉴权在服务端：**密钥永不下发给别人**，别人只是"能用这个库去检索"。

对外只暴露两个函数：
    mine(store, uid)                        → 我自己的那些（原样，含密钥）
    usable(store, uid, name, is_admin)      → 我这次能用的（自己的 + 别人共享给我的），
                                              每条带 `_owner` / `_from_share` 标记
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("eg2.difyshare")

SHARE_PRIVATE = "private"
SHARE_ALL = "all"
SHARE_TO = "to"


def _load(raw: str | None) -> list[dict]:
    """把偏好里的 JSON 解析成 list（坏数据当成空，绝不抛）。"""
    try:
        data = json.loads(str(raw or "[]"))
    except Exception:                                            # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if isinstance(it, dict) and it.get("dataset_id"):
            out.append(dict(it))
    return out


def _share_of(item: dict) -> str:
    s = str(item.get("share") or SHARE_PRIVATE).strip().lower()
    return s if s in (SHARE_PRIVATE, SHARE_ALL, SHARE_TO) else SHARE_PRIVATE


def names_of(item: dict) -> list[str]:
    raw = item.get("share_to") or []
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.replace("，", ",").split(",")]
    return [str(x).strip() for x in raw if str(x).strip()]


def mine(store, uid: int) -> list[dict]:                         # noqa: ANN001
    """我自己配的那些库（含密钥，只在服务端用）。"""
    prefs = store.prefs_all(int(uid or 0), inherit_sensitive=True)
    return _load(prefs.get("eg2-difylist"))


def visible_to(store, uid: int, username: str) -> list[dict]:     # noqa: ANN001
    """别人共享给我的库：share=all，或 share=to 且名单里有我。

    返回的每条都带 `_owner`（谁共享的）和 `_from_share=True`；
    密钥**照样带着**（服务端要拿它去查 Dify），但绝不会出现在界面接口里 ✓
    """
    me = str(username or "").strip()
    if uid <= 0 or not me:
        return []
    rows = store._query(                                    # noqa: SLF001
        "SELECT user_id, value FROM user_prefs "
        "WHERE key='eg2-difylist' AND user_id<>?", (int(uid),))
    out = []
    for r in rows:
        owner_id = int(r["user_id"])
        owner_name = _user_name(store, owner_id)
        for it in _load(r["value"]):
            s = _share_of(it)
            if s == SHARE_ALL or (s == SHARE_TO and me in names_of(it)):
                it = dict(it)
                it["_owner"] = owner_name or ("#%d" % owner_id)
                it["_from_share"] = True
                out.append(it)
    return out


def usable(store, uid: int, username: str) -> list[dict]:         # noqa: ANN001
    """这一次检索能用的全部库：我自己的（含我标了共享的）+ 别人共享给我的。"""
    out = []
    for it in mine(store, uid):
        it = dict(it)
        it.setdefault("_owner", username or "我")
        it["_from_share"] = False
        out.append(it)
    out.extend(visible_to(store, uid, username))
    # 关掉的不参与检索（但保留在列表里，方便用户看到自己关了什么）
    return [x for x in out if x.get("enabled", True) is not False]


def _user_name(store, uid: int) -> str:                          # noqa: ANN001
    try:
        from .auth import Auth

        u = Auth(store.path).get(int(uid))
        return str((u or {}).get("name") or "")
    except Exception:                                            # noqa: BLE001
        return ""


def resolve_names(store, names: list[str]) -> tuple[list[str], list[str]]:  # noqa: ANN001
    """把用户填的名字分成「存在的」和「查无此人」两类（保存时校验用）。"""
    ok, bad = [], []
    try:
        from .auth import Auth

        auth = Auth(store.path)
        for n in names:
            n = str(n).strip()
            if not n:
                continue
            (ok if auth.get_by_name(n) else bad).append(n)
    except Exception:                                            # noqa: BLE001
        bad.extend(names)
    return ok, bad


def describe(items: list[dict], viewer: str) -> list[dict]:
    """给界面看的描述（**去掉密钥**）：名字 / 共享状态 / 谁共享给我的。"""
    out = []
    for it in items:
        d = {k: v for k, v in it.items() if k not in ("dataset_key",)}
        out.append(d)
    return out
