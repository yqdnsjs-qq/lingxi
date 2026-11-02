"""按用户挑库（多人版地基，2026-10-20）。

设计原则：**惰性启用，行为不变**
------------------------------
现在整站还是一个库（`data/index/index.db`）。多人化以后要变成
`data/users/<用户id>/index.db`，但一步到位风险太大（存储、检索、同步全要改）。

所以这里先只做"**挑库**"这一层：
  · 某个用户如果已经有自己的库文件 → 就用他自己的
  · 还没有 → **回落到主库**（也就是现在的行为，一模一样）✓

这样：
  · 今天不改任何调用点，系统跑得和以前一样（零风险）
  · 以后把用户库建出来（迁移工具），**请求路径不用改**就能各用各的
  · 出问题只要把 `data/users/` 挪走，立刻退回单库模式 ✓

为什么要 LRU：一个 Store 实例 = 一个 SQLite 连接 + 一份 FTS 状态，
每个都占几十 MB 常驻。人多了不能全开着，最多同时留 `MAX_OPEN` 个。

⚠ 已知限制（下一步要解决）：
  · 检索目前只查**一个**库；真正的多人要"我的库 + 共享给我的库 + 公共库"
    做联邦查询（见 docs\\方案-多用户独立库与外网-20261020.md 阶段 1）
  · 后台任务（打标/索引/备份）没有"当前用户"的概念，仍然用主库
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .store import Store

log = logging.getLogger("eg2.userdb")

# 同时最多开几个用户的库（多了就关掉最久没用的）
MAX_OPEN = 4

_lock = threading.RLock()
_pool: dict[int, Store] = {}
_order: list[int] = []          # 最近使用顺序（末尾最新）


def user_db_path(cfg, user_id: int) -> Path | None:
    """这个用户自己的库文件（没有就返回 None = 该用主库）✓"""
    uid = int(user_id or 0)
    if uid <= 0:
        return None
    p = Path(cfg.data_dir) / "users" / str(uid) / "index.db"
    return p if p.exists() else None


def db_path_for(cfg, user_id: int = 0) -> Path:
    """该用哪个库文件：有用户库用用户的，没有用主库 ✓"""
    return user_db_path(cfg, user_id) or Path(cfg.db_path)


def is_split(cfg, user_id: int = 0) -> bool:
    """这个用户是不是已经"独立成库"了（界面上可以据此显示提示）"""
    return user_db_path(cfg, user_id) is not None


def store_for(cfg, user_id: int = 0) -> Store:
    """拿这个用户该用的 Store（带 LRU，最多同时开 MAX_OPEN 个）✓

    注意：uid=0 或没有用户库时，返回的是**主库**的实例；
    主库实例由调用方（server.py）自己持有，这里不重复创建。
    """
    path = db_path_for(cfg, user_id)
    main = Path(cfg.db_path)
    if path == main:
        # 主库：交给调用方，本模块不管（避免两个连接写同一个文件）
        raise LookupError("这个用户用的是主库，请由调用方提供主库实例")
    uid = int(user_id)
    with _lock:
        st = _pool.get(uid)
        if st is not None:
            if uid in _order:
                _order.remove(uid)
            _order.append(uid)
            return st
        st = Store(cfg, db_path=path)
        _pool[uid] = st
        _order.append(uid)
        while len(_order) > MAX_OPEN:
            old = _order.pop(0)
            victim = _pool.pop(old, None)
            if victim is not None:
                try:
                    victim.close()
                except Exception as exc:                         # noqa: BLE001
                    log.warning("关闭用户 %s 的库失败：%s", old, exc)
                log.info("多人库：关掉用户 %s 的库（超过 %d 个）", old, MAX_OPEN)
        log.info("多人库：打开用户 %s 的库 %s", uid, path)
        return st


def close_all() -> None:
    """关掉所有用户库（停服务/热重载时用）✓"""
    with _lock:
        for uid, st in list(_pool.items()):
            try:
                st.close()
            except Exception:                                    # noqa: BLE001
                pass
            log.info("多人库：已关闭用户 %s 的库", uid)
        _pool.clear()
        _order.clear()


def status(cfg) -> dict:
    """给管理界面看：哪些用户已经独立成库、哪些还在主库上 ✓"""
    users_dir = Path(cfg.data_dir) / "users"
    rows = []
    if users_dir.is_dir():
        for d in sorted(users_dir.iterdir()):
            if not d.is_dir() or not d.name.isdigit():
                continue
            db = d / "index.db"
            mb = round(db.stat().st_size / 1e6, 1) if db.exists() else 0.0
            rows.append({"user_id": int(d.name), "has_db": db.exists(), "mb": mb})
    with _lock:
        opened = sorted(_pool.keys())
    return {"split": bool(rows), "users": rows, "opened": opened,
            "max_open": MAX_OPEN, "main_db": str(cfg.db_path)}
