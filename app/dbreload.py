"""索引库热重载：让**所有**持有 Store 的地方一起换到新连接。

为什么需要这个模块：Store 在项目里被好几处各持一份 ——
  · 搜索路由（filesearch.create_router 里的 store / searcher）
  · 设置路由（它自己也收到一份 store）
  · Indexer（self.store）
  · app.state.store / app.state.indexer
  · engine.modality_provider 那个闭包（读 create_app 作用域里的 store）
换库时只重绑其中一处，其它地方就继续握着**已经关闭**的旧连接，报
    sqlite3.ProgrammingError: Cannot operate on a closed database
实测表现就是：换库之后「设置」页 500。

所以这里改成"各作用域自己登记一个换连接的回调"：
    换库 → dbreload.notify(新 Store) → 每个登记者把自己的引用换掉
登记用 name 去重，重复登记只留最后一个（反复创建 app 也不会堆钩子）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

_HOOKS: list[tuple[str, Callable[[Any], None]]] = []


def register(name: str, fn: Callable[[Any], None]) -> None:
    """登记一个「把内部引用换成新 Store」的回调。"""
    global _HOOKS
    _HOOKS = [(n, f) for (n, f) in _HOOKS if n != name]
    _HOOKS.append((name, fn))


def names() -> list[str]:
    return [n for n, _f in _HOOKS]


def notify(new_store: Any) -> dict[str, str]:
    """把新 Store 交给所有登记者，返回每处的结果（写进接口响应，方便排错）。"""
    out: dict[str, str] = {}
    for name, fn in list(_HOOKS):
        try:
            fn(new_store)
            out[name] = "ok"
        except Exception as exc:                                  # noqa: BLE001
            out[name] = f"失败：{exc}"
            log.warning("热重载：%s 换连接失败：%s", name, exc)
    if not out:
        log.warning("热重载：没有任何登记者 —— 可能有作用域没登记，检查一下")
    return out
