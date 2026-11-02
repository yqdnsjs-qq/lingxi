"""路径规范化的安全封装（网络盘陷阱，2026-10-17）。

为什么单独一个模块：`Path.resolve()` 会做 realpath ✓，而**连不上 / 凭据失效的
网络盘**（实测 `Z:\\Pro-Calibre`）会抛
`OSError: [WinError 1326] 用户名或密码不正确` ✗。
调用方只要没兜住，整个请求就是 500「Internal Server Error」✗
—— 实测就是"添加文件夹"时浏览目录炸的（项目主报的那个错）。

这类规范化散在 6 处以上，所以**统一放这里一份** ✓（免得改了这处漏那处 ✗）。
"""
from __future__ import annotations

import os
from pathlib import Path


def safe_resolve(p) -> Path:                                     # noqa: ANN001
    """尽量 `resolve()`；**算不出来就退回 `abspath`** ✓（绝不抛异常）。"""
    try:
        return Path(p).resolve()
    except OSError:
        return Path(os.path.abspath(str(p)))


def norm_key(p) -> str:                                          # noqa: ANN001
    """规范化成"大小写不敏感的绝对路径"字符串。

    **纯字符串运算、不碰磁盘** ✓ —— 用来比较"这个目录是不是已经在索引里"，
    比 `resolve()` 又快又不会因为网络盘挂掉而报错 ✓。
    """
    return os.path.normcase(os.path.abspath(str(p)))
