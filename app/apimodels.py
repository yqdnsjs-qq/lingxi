"""哪些外部 API 配置"这个用户能用"（聊天页 / 知识库问答页共用一套规则）。

★ 2026-10-22 项目主报的 bug：
    普通用户登录**服务器**后，聊天和知识库都选不到服务器上那台 **llama.cpp**
    （它没密钥，本来就是公共推理资源），管理员的「共享模型」按钮也点不动。

根因两条：
  ① 配置存在**管理员自己的 user_prefs** 里，别人的 `prefs_all()` 看不到；
  ② 两个页面各自用自己的下标去找配置，下标对不上就报"没共享给你"。

规则（和界面「谁能用」一致）：
    · 我自己的       → 全部可用（含带密钥的）
    · 别人的·无密钥  → 公共资源，所有登录用户可用 ✓（服务器 llama.cpp 就是这种）
    · 别人的·带密钥  → 只有 `share=true`（所有人）或 `share_to` 里点名有我
密钥只在服务端用，前端只拿模型名 ✓
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("eg2.apimodels")


def _name_of(store, uid: int) -> str:                             # noqa: ANN001
    try:
        from .auth import Auth

        u = Auth(store.path).get(int(uid))
        return str((u or {}).get("name") or ("#%d" % int(uid)))
    except Exception:                                            # noqa: BLE001
        return "#%d" % int(uid)


def items_for(store, uid: int, viewer_name: str = "") -> list[dict]:  # noqa: ANN001
    """返回这个用户**这次能用**的 API 配置列表；`api:N` 的 N 就是这里的下标。

    每项额外带：`_owner`（谁的）、`_own`（是不是我的）、`_public`（有没有密钥）
    """
    uid = int(uid or 0)
    out: list[dict] = []
    seen: set[tuple] = set()

    def _add(x: dict, owner: str, own: bool) -> None:
        key = (str(x.get("base_url") or "").strip(),
               str(x.get("model") or "").strip(),
               str(x.get("api_key") or "")[:8])
        if not key[0] or not key[1] or key in seen:
            return
        seen.add(key)
        y = dict(x)
        y["_owner"] = owner or "?"
        y["_own"] = own
        y["_public"] = not str(x.get("api_key") or "").strip()
        out.append(y)

    # ① 自己的
    try:
        own = store.prefs_all(uid, inherit_sensitive=False)
    except Exception:                                            # noqa: BLE001
        own = {}
    try:
        mine = json.loads(str(own.get("eg2-rmapilist") or "[]"))
    except Exception:                                            # noqa: BLE001
        mine = []
    for x in (mine if isinstance(mine, list) else []):
        if isinstance(x, dict):
            _add(x, viewer_name or "我", True)

    # ② 别人的（公共的 / 共享给我的）
    if uid > 0:
        try:
            rows = store._query(                                # noqa: SLF001
                "SELECT user_id, value FROM user_prefs "
                "WHERE key='eg2-rmapilist' AND user_id<>?", (uid,))
        except Exception:                                        # noqa: BLE001
            rows = []
        for r in rows:
            owner = _name_of(store, int(r["user_id"]))
            try:
                lst = json.loads(str(r["value"] or "[]"))
            except Exception:                                    # noqa: BLE001
                continue
            for x in (lst if isinstance(lst, list) else []):
                if not isinstance(x, dict):
                    continue
                has_key = bool(str(x.get("api_key") or "").strip())
                to = [int(v) for v in (x.get("share_to") or [])
                      if str(v).strip().isdigit()]
                # 新版界面存的是**用户名**（share_to_names），老记录存的是 id ✓
                names = x.get("share_to_names") or []
                if isinstance(names, str):
                    names = [y.strip() for y in names.replace("，", ",").split(",")]
                names = [str(y).strip() for y in names if str(y).strip()]
                me = str(viewer_name or "").strip()
                if has_key and not x.get("share") and uid not in to \
                        and (not me or me not in names):
                    continue
                _add(x, owner, False)
    return out


def by_index(store, uid: int, idx: int, viewer_name: str = "") -> dict | None:  # noqa: ANN001
    items = items_for(store, uid, viewer_name)
    if 0 <= int(idx) < len(items):
        return items[int(idx)]
    return None


def headers_of(item: dict | None) -> dict | None:
    """把配置里的 `headers` 变成真正的 dict。

    ⚠ 2026-10-23 报的 bug：知识库问答选外部模型报
    `外部模型调用失败：'str' object has no attribute 'items'`
    原因：界面存进偏好里的 `headers` 是**JSON 字符串**，老路径前端会先 JSON.parse
    再传，所以没事；新路径（按序号解析 api:N）直接把字符串当 dict 传给
    `vlm_client._headers(extra)` → 那里对它调 `.items()` 就炸了 ✗
    这里统一解析：dict 原样、字符串按 JSON 解、解不出来就当没有 ✓
    """
    if not item:
        return None
    raw = item.get("headers")
    if isinstance(raw, dict):
        return raw or None
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        d = json.loads(s)
    except Exception:                                            # noqa: BLE001
        return None
    return d if isinstance(d, dict) else None
