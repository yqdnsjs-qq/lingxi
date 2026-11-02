"""文件搜索的 HTTP 接口（挂在同一个服务上）。

搜索页：/search       接口：/api/...
"""
from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time                     # 预览排除 / 从文件夹里选 要用它做时间上限 ✓
from pathlib import Path
from typing import Any

import numpy as np

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import desktop
from . import difyclient          # 知识库问答页的「☁ Dify」开关要用（2026-10-22）
from . import extract
from .config import Config

# 项目根目录（本模块里多处要用，统一在这里定义）
ROOT = Path(__file__).resolve().parent.parent
from .folderconf import FolderConfig
from .folderconf import KEYS as FOLDER_KEYS
from . import folderconf as FC
# 关键词切分**只此一份**（在 ftsindex 里）：检索、定位、问答兜底全用它 ✓
# 上次的坑：这段逻辑在两处各写了一份，改了本机那条、漏了服务器那条 ✗
from .ftsindex import query_keywords as _query_keywords
from .pathutil import safe_resolve
from .indexer import Indexer
from .model import EmbeddingEngine, InputError, decode_image
from .searcher import Searcher
from .store import Store, _from_blob

log = logging.getLogger("eg2.filesearch")


def _can_index_here() -> bool:
    """这台机器是不是「建库机」（索引的权威那一台）。

    判据和同步脚本用的是同一个：项目里有没有 `run/deploy/sync_db.py` ✓
      · 有  → 本机（有显卡、索引以它为准）→ **用户自己就能跑索引** ✓
      · 没有 → 服务器（索引在这里建，下次同步会被整份覆盖 ✗）
              → 要**管理员**才能跑 ✓（项目主 2026-10-16 明确的规则）

    ⚠ 两处判据必须一致（`app/sync_api.py::can_run_here` 也是看这个文件 ✓）——
      改一处记得改另一处。
    """
    return (ROOT / "run" / "deploy" / "sync_db.py").is_file()


def _local_open(strict: bool) -> bool:
    """本机是否**不限制数据可见性**（A 方案）。

    项目主 2026-10-21 定的口径：
      · `[multi] local_strict = yes`（**默认**，B 方案）
          → 本机也按归属隔离：普通用户只看自己名下 + 共享的 ✓
      · `local_strict = no`（A 方案）
          → 本机 = 个人专用，任何登录的人都看到全部
    只有"本机 + 不严格"才放开；服务器永远按归属走 ✓
    """
    return _can_index_here() and not bool(strict)


# ★ 2026-10-20 项目主："本地版是希望个人专用（应该有管理员版可以统管全局，
#   与个人版区别）"
#   同一套代码、两种形态，靠登录的人 + 这台机器是不是服务器来判定：
#     personal = **个人版**（本机，就我自己一个人用）
#                一切以"我"为中心：能建索引、能打标、能备份，**不显示多人管理**
#     admin    = **管理员版**（服务器，或本机上有多个用户且我是管理员）
#                统管全局：用户管理、共享设置、索引、打标、备份、快照
#     member   = **成员版**（服务器上的普通用户）
#                只看自己的 + 别人共享的；打标/备份/索引都不能碰
#   为什么这么分而不是做成两个安装包：一套代码少一半维护量，升级不用装两次 ✓
_VIEW_LABELS = {
    "personal": "个人版",
    "admin": "管理员版",
    "member": "成员",
}


def _view_mode(role: str = "", can_index: bool | None = None) -> str:
    """这台机器 + 当前登录的人 → 该给哪种界面 ✓

    ⚠ 2026-10-20 踩到：最初这个函数自己去调 `_who(request)`，但 `_who` 是
      `create_router()` 里的**闭包函数**，模块级根本看不见 → NameError 被吞掉
      → 服务器上管理员也判成"成员" ✗
      所以改成**纯函数**：角色由调用方传进来 ✓

    判据只有一条：**这台是不是服务器**（即"是不是建库机"）。
      · 本机（建库机）→ 永远 `personal`：这台就是"你自己的电脑"，
        不该出现"用户管理 / 共享设置"这类统管入口 ✓
      · 服务器 → 看角色：admin 给管理员版，其他人给成员版。
    """
    if can_index is None:
        can_index = _can_index_here()
    _role = str(role or "").strip().lower()
    if can_index:
        # ★ 2026-10-22 项目主澄清："本机版用户或管理员应该**唯一区别是能不能
        #   管理操作服务器**而已，但本机建库打标应该都可以用才行"
        #   → 本机**不分角色**，一律 personal（建库/索引/打标/设置全开）✓
        #     数据可见性仍按 `local_strict` 走（默认只看自己名下 + 共享的）
        return "personal"
    return "admin" if _role == "admin" else "member"


def _safe_isdir(path: str) -> bool:
    """断开的网络驱动器会让 stat 抛 PermissionError，这里统一兜底。"""
    try:
        return os.path.isdir(path)
    except OSError:
        return False


# 浏览器能直接渲染的类型（mimetypes 不一定认识 heic/avif，这里补全）
MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
    ".jfif": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".bmp": "image/bmp", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".ico": "image/x-icon",
    ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".aac": "audio/aac",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
}


def attachment_header(name: str) -> str:
    """中文文件名要用 RFC 5987 的 filename* 才不会乱码。"""
    from urllib.parse import quote

    stem, dot, ext = name.rpartition(".")
    keep = lambda s: s.encode("ascii", "ignore").decode().strip(" ._-")
    ascii_ext = keep(ext) if dot else ""
    ascii_stem = keep(stem) if dot else keep(name)
    if not ascii_stem:
        ascii_stem = "download"
    ascii_name = f"{ascii_stem}.{ascii_ext}" if ascii_ext else ascii_stem
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _open_path(path: Path, reveal: bool) -> str:
    """reveal=True 时在资源管理器里「选中」这个文件，否则用默认程序打开。"""
    target = os.path.normpath(str(path))
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail=f"文件不存在或无法访问：{path}")

    if reveal:
        problem = desktop.reveal_in_explorer(target)
        if not problem:
            return "已在资源管理器中定位"
        log.warning("Shell 定位失败（%s），改用 explorer 命令再试", problem)
        try:
            desktop.open_in_file_manager(path)
        except OSError as exc:
            raise HTTPException(status_code=500,
                                detail=desktop.failure_hint(problem)) from exc
        return ("已尝试调起资源管理器；如果弹出 explorer.exe 报错，说明当前环境"
                "不允许后台进程操作桌面 —— " + desktop.failure_hint(problem))

    try:
        return desktop.open_with_default(target)
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=desktop.failure_hint(str(exc))) from exc


_KB_NOISE_CHARS = "%&=<>\\{}[]|"


def _gpu_free_mb() -> int | None:
    """显卡还剩多少显存（MB）。拿不到就返回 None。"""
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:                                     # noqa: BLE001
        return None


def _no_kb_hint() -> str:
    """"没有可用知识库"时的提示。

    2026-10-16 项目主纠正：老提示只对**管理员**说得通
    （"在文件夹那一行点「配置」把「作为知识库」打开"），普通用户看到它一头雾水 ✗
    —— 他既没有那个文件夹的配置入口，也不该自己去建索引。
    现在一句话把两种情况都说清楚：管理员怎么开、普通用户该找谁 ✓
    """
    return ("当前账号没有可用的知识库文件夹。\n"
            "· 如果你是管理员：在「文件夹」那一行点「配置」，把「作为知识库」打开，"
            "并把那个文件夹设为**共享**（共享是给全体的，私有只给自己 ✓）。\n"
            "· 如果你是普通用户：请让管理员把知识库文件夹设为「共享」——"
            "共享之后你就能直接提问，不需要重新扫描索引 ✓。")


def _kb_is_noise(text: str) -> bool:
    """判断一个片段是不是网页噪声（URL / SVG / base64 / 导航文字）。"""
    import re as _re

    if len(text) < 40:
        return True
    n = len(text)
    # URL 编码串（%2F %3F %E4%B8 这类）密度高 → 基本是图片链接/参数
    pct = len(_re.findall(r"%[0-9A-Fa-f]{2}", text))
    if pct >= 6 or pct / n > 0.012:
        return True
    # 长得像 URL 的参数串：&xxx=yyy 出现多次
    if len(_re.findall(r"[?&][a-zA-Z_]{2,20}=", text)) >= 3:
        return True
    # 查询参数关键词（剪藏常见的图片链接尾巴）
    if sum(text.count(k) for k in ("exptype=", "wx_fmt=", "tp=webp",
                                   "from=appmsg", "fasttmpl", "&amp;")) >= 3:
        return True
    # 编码符号占比过高
    bad = sum(text.count(c) for c in _KB_NOISE_CHARS)
    if bad / n > 0.10:
        return True
    # 超长无空格串（base64 / data URI / 哈希）
    if _re.search(r"[A-Za-z0-9+/=_-]{90,}", text):
        return True
    # 拉丁字母占比过高而中文很少 → 多半是链接/代码/参数，不是正文
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if latin / n > 0.55:
        return True
    # 中文太少：正常中文文章里中文字符占比应该不低
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk / n < 0.12:
        return True
    # 明显的剪藏头尾
    if text.lstrip().startswith(("---", "原始链接", "![[", "<")):
        head = text[:120]
        if "原始链接" in text[:200] or "标题:" in head:
            return True
    return False


# ---------------------------------------------------------------- PDF 原版式
# 索引里存的是「各页文字用换行拼起来」的一整段，页码在抽取阶段就丢了，
# 表格的版式（合并单元格、对齐）更不在文字层里。所以预览时按页重读一遍：
#   文字分页 —— pypdf 就够（已有依赖）
#   页面出图 —— 需要 PyMuPDF，没装就只给文字，并且明确告诉用户怎么装
_PDF_PAGE_MAX = 400          # 一次最多返回多少页（防超大 PDF 卡住浏览器）
_PDF_PAGE_CHARS = 6000       # 单页最多给多少字


def pdf_render_available() -> bool:
    try:
        import fitz  # noqa: F401
    except Exception:                                          # noqa: BLE001
        return False
    return True


def private_folder_ids(store) -> set[int]:
    """哪些文件夹被标了「🔒 隐私」（内容**永不发给外部模型**）✓

    项目主 2026-10-20："我的隐私文件统统放在里面，这样设置更方便"
                       + "这样就是用外部模型也搜索不到"
    ⚠ 这**不是** `visibility=private`（那是"普通用户看不到"）✗
      这里是"连管理员自己用外部模型问答时，这些片段也不出机器" ✓
    本机搜索仍照常能搜到（不然这个文件夹就没法用了）✓
    """
    try:
        rows = store._query("SELECT id FROM folders WHERE IFNULL(privacy, 0) = 1")
        return {int(r["id"]) for r in rows}
    except Exception as exc:                                     # noqa: BLE001
        log.warning("读隐私文件夹失败（按「没有隐私文件夹」处理）：%s", exc)
        return set()


def caption_model_cfg(store) -> dict | None:
    """读出「外部打标模型」的配置（后台任务用；模块级，脚本能直接测）✓

    ⚠ 为什么不能像 /api/caption/start 那样从 payload 拿：
      「全力执行」是**后台线程**在跑，没有前端请求 ✗
      → 只能读库里存着的偏好

    ★ 2026-10-20 修（项目主发现的真 bug）：
      他设的是「打标模型 = glm-5.3-flash」，但打标跑出来的是 deepseek 的推理文本 ——
      一查：界面上的「打标模型」是**另一套设置**（`ui_pref.eg2-vl*`），
      而这里读的是「外部模型」卡（`ui_pref.eg2-rm*`，那是**问答**用的）✗
      两条路径读两份偏好 → 用户怎么改打标模型都不生效 ✗
      → 改成**先读打标设置**（eg2-vlmodel + eg2-vlmodelname + eg2-rmapilist 那一套），
        读不到再回落到老的 eg2-rm* ✓
      「打标模型」下拉的语义（见 search.html 的 vlModelChoices）：
        'local'      → 用本机视觉模型 → 这里返回 None，让调用方走本地
        'remote:<i>' → 用「外部模型」卡里第 i 套端点（0 起）
                       模型名**以手填的 eg2-vlmodelname 为准** ✓
    """
    def _p(name: str, default: str = "") -> str:
        try:
            v = store.get_meta("ui_pref." + name)
        except Exception:                                        # noqa: BLE001
            v = None
        return str(v) if v else default

    def _headers_of(raw: str) -> dict | None:
        if not str(raw or "").strip():
            return None
        try:
            got = json.loads(raw)
            return got if isinstance(got, dict) else None
        except Exception as exc:                                 # noqa: BLE001
            log.warning("自定义请求头解析失败（忽略）：%s", exc)
            return None

    # ---- ① 先按「打标模型」下拉来（项目主真正在改的那一项）----
    pick = _p("eg2-vlmodel")
    if pick.startswith("remote:"):
        try:
            idx = int(pick.split(":", 1)[1] or 0)
        except (TypeError, ValueError):
            idx = -1
        try:
            suites = json.loads(_p("eg2-rmapilist", "[]") or "[]")
        except Exception:                                        # noqa: BLE001
            suites = []
        if 0 <= idx < len(suites) and isinstance(suites[idx], dict):
            suite = suites[idx]
            url = str(suite.get("base_url") or "").strip()
            # 手填的模型名优先（同一个网关可以填不同模型，界面就是这么设计的）✓
            model = (_p("eg2-vlmodelname").strip()
                     or str(suite.get("model") or "").strip())
            if url and model:
                log.info("插图打标用的是「打标模型」设置：%s @ %s（外部第 %d 套）",
                         model, url, idx)
                return {"base_url": url, "model": model,
                        "api_key": str(suite.get("api_key") or ""),
                        # 界面里打标这套写死 448/800（和 /api/vl/caption 一致）
                        "max_side": 448, "max_tokens": 800,
                        "headers": _headers_of(str(suite.get("headers") or ""))}
        log.warning("打标模型选了外部第 %s 套，但「外部模型」卡里没有这一套"
                    "（eg2-rmapilist 只有 %d 套）→ 回落到老的设置",
                    idx, len(suites) if isinstance(suites, list) else 0)
    elif pick == "local":
        # 明确选了本机视觉模型 → 不要用外部 ✓
        log.info("插图打标用的是本机视觉模型（界面里选了「本机」）")
        return None

    # ---- ② 回落：老的 eg2-rm*（问答那套外部模型）----
    url = _p("eg2-rmurl")
    model = _p("eg2-rmmodel")
    if not (url and model):
        return None
    try:
        side = int(_p("eg2-rmside", "768") or 768)
        tok = int(_p("eg2-rmtok", "220") or 220)
    except (TypeError, ValueError):
        side, tok = 768, 220
    # ★ 自定义请求头（2026-10-19 实测踩到）：opencode 必须要
    #   `x-opencode-session`，否则回 HTTP 400 MissingSessionID ✗
    log.info("插图打标回落用「外部模型」卡：%s @ %s", model, url)
    return {"base_url": url, "model": model, "api_key": _p("eg2-rmkey"),
            "max_side": side, "max_tokens": tok,
            "headers": _headers_of(_p("eg2-rmhdr"))}


# 「计划 / 全力执行」的模块级状态（2026-10-19）
# ⚠ 别用 app.state ✗ —— filesearch.py 是 create_router() 工厂，这里根本没有 app
#   （实测踩过：NameError: name 'app' is not defined，接口 500）
_LAST_PLAN: dict | None = None
_NIGHT: dict = {"running": False, "note": "", "started": 0.0}


def create_router(cfg: Config, store: Store, engine: EmbeddingEngine, indexer: Indexer) -> APIRouter:
    router = APIRouter()
    searcher = Searcher(cfg, store, engine)
    static_dir = Path(__file__).resolve().parent / "static"

    # 换库时把自己这两个引用一起换掉（原因见 app/dbreload.py 的模块说明：
    # 只重绑一处的话，别的作用域会继续用已关闭的旧连接，设置页就 500）
    from .dbreload import register as _register_reload

    def _rebind_db(new_store) -> None:
        nonlocal store, searcher
        store = new_store
        searcher = Searcher(cfg, new_store, engine)

    _register_reload("search-router", _rebind_db)

    # ---- 数据隔离（多人 P2-b）----
    def _visible_folders(request) -> set[int] | None:
        """当前用户能看到的文件夹 id 集合；None = 不限制。

        规则（见 docs/多人使用设计.md）：
          · 管理员 → 不限制 ✓
          · 普通用户 → 共享库(owner_id=0 且 visibility=shared) + 自己的私有 + ACL 例外
          · **没登录 / 还没启用账号体系** → 只给共享库 ✓
            （安全默认：匿名看不到别人的私有文件夹；现有文件夹全共享 → 老行为不变 ✓）
        出错一律**退回"只给共享库"**，不会因为异常把别人的私有数据漏出去 ✓
        """
        # ★ 2026-10-21 项目主："本机版用户与管理员的区别只有能不能管理服务器"
        #   本机（建库机）= 这台机器上的数据都是他自己的 → 不限制 ✓
        #   服务器上仍然按 归属 / 共享 / ACL 收窄 ✓
        if _local_open(cfg.local_strict):
            return None
        try:
            from .multiuser import visible_folder_ids as _vis

            ids = _vis(cfg.db_path, _who(request))
            return None if ids is None else set(int(x) for x in ids)
        except Exception as exc:                                  # noqa: BLE001
            log.warning("取可见文件夹失败，按「只看共享库」处理：%s", exc)
            try:
                from .multiuser import visible_folder_ids as _vis2

                return set(int(x) for x in (_vis2(cfg.db_path, None) or []))
            except Exception:                                     # noqa: BLE001
                return set()

    def _scope_kb(ids: list[int], request) -> list[int]:
        """把知识库文件夹范围收窄到"这个用户能看到的"（问答 / Dify 都用）。

        和搜索用同一套规则（`visible_folder_ids` ✓）：管理员不限制 ✓，
        普通用户与匿名只看共享库 + 自己的私有 + ACL 例外 ✓。
        出错时**退回"只给共享库"**，宁少给不多给 ✓。
        """
        try:
            from .multiuser import visible_folder_ids as _vis

            try:
                from .auth import COOKIE_NAME as _COOKIE, Auth as _Auth

                u = _Auth(cfg.db_path).who(request.cookies.get(_COOKIE))
            except Exception:                                     # noqa: BLE001
                u = None
            vis = _vis(cfg.db_path, u)
            if vis is None:
                return list(ids)                     # 管理员：不限制 ✓
            keep = set(int(x) for x in vis)
            out = [int(x) for x in ids if int(x) in keep]
            if len(out) != len(ids):
                log.info("知识库隔离：范围从 %d 个文件夹收窄到 %d 个", len(ids), len(out))
            return out
        except Exception as exc:                                  # noqa: BLE001
            log.warning("知识库隔离失败，按「只看共享库」处理：%s", exc)
            try:
                from .multiuser import visible_folder_ids as _vis2

                keep = set(int(x) for x in (_vis2(cfg.db_path, None) or []))
                return [int(x) for x in ids if int(x) in keep]
            except Exception:                                     # noqa: BLE001
                return []

    # ---- 人物 / 人脸**列表**接口的可见性过滤（多人 P2-b 收尾）----
    # 为什么必须改端点、不能靠中间件：中间件改不了响应体 ✗（见 preview_guard.py
    # 的说明）。所以列表类接口统一在端点里按 `files.folder_id ∈ 可见集合` 过滤 ✓。
    # 规则与搜索/问答完全一致（都走 _visible_folders）：管理员不限制，其他人
    # 只看共享库 + 自己的私有 + ACL 例外；出错一律退回"只给共享库" ✓。
    def _ph(ids) -> tuple[str, list[int]]:                        # noqa: ANN001
        vals = [int(x) for x in ids]
        return ",".join("?" * len(vals)), vals

    def _people_scoped(visible: set[int], only_named: bool = True) -> list[dict]:
        """普通用户看到的人物列表（管理员不走这里 ✓）。

        **2026-10-17 项目主要求，规则改了**（老规则见下面的历史注释）：
          · 只显示**已命名**的人物 ✓（未命名的不出现在用户端 ✗）
          · **名字与头像跟管理员保持一致** ✓ —— 头像直接用管理员设的
            `people.cover_face` ✓，不再"按可见范围重算" ✗
            （那正是项目主看到"用户端头像不一样"的原因：重算会挑到另一张脸）
          · 张数 / 文件数仍按**可见范围**现算 ✓（别把私有照片的数量露出去 ✗）
          · 可见范围内一张脸都没有的人 → 不显示 ✓（点进去也是空的 ✗）

        历史注释（老规则）：`people` 表本身没有文件夹字段 ✗ —— 一个人的脸可能横跨
        私有和共享两个文件夹，所以「这个人有几个文件」必须按可见范围现算 ✓；
        「拿哪张脸当封面」以前也现算，是为了不告诉外人"这人本来有张私有照片" ✗。
        现在按项目主的要求改成全局一致 —— 头像属于"人物元数据" ✓，
        它的缩略图在 `preview_guard` 里对"已命名人物的封面"单独放行 ✓。
        """
        if not visible:
            return []                                            # 什么都看不到 → 没有人物 ✓
        ph, vals = _ph(visible)
        rows = store._query(
            "SELECT p.id, p.name, p.kind, p.cover_face AS g_cover, "
            "       COUNT(fa.id) AS v_faces, "
            "       COUNT(DISTINCT fa.file_id) AS v_files, "
            "       (SELECT fa2.id FROM faces fa2 JOIN files f2 ON f2.id = fa2.file_id "
            "         WHERE fa2.person_id = p.id AND f2.folder_id IN (" + ph + ") "
            "         ORDER BY (fa2.prob * (fa2.x2-fa2.x1) * (fa2.y2-fa2.y1)) DESC "
            "         LIMIT 1) AS v_cover "
            "FROM people p JOIN faces fa ON fa.person_id = p.id "
            "JOIN files f ON f.id = fa.file_id "
            "WHERE f.folder_id IN (" + ph + ") "
            "GROUP BY p.id ORDER BY v_faces DESC, p.id", vals + vals)
        out: list[dict] = []
        for r in rows:
            name = str(r["name"] or "").strip()
            if only_named and not name:
                continue                                         # 未命名 → 用户端不显示 ✓
            if int(r["v_faces"] or 0) <= 0:
                continue                                         # 他可见范围内没有脸 ✓
            g_cover = int(r["g_cover"] or 0)
            out.append({"id": int(r["id"]), "name": r["name"], "kind": r["kind"],
                        "n_faces": int(r["v_faces"] or 0),
                        "n_files": int(r["v_files"] or 0),
                        # 头像：优先用**管理员设的那张** ✓（全局一致 ✓）
                        # 管理员没设过 → 退回"可见范围内最清楚的一张" ✓
                        "cover_face": g_cover or r["v_cover"]})
        return out

    def _face_stats_scoped(visible: set[int]) -> dict:
        """容量统计也按可见范围现算（/api/faces/status、backfill 返回体）。"""
        if not visible:
            return {"faces": 0, "files": 0, "named_people": 0}
        ph, vals = _ph(visible)
        row = store._query(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT fa.file_id) AS f FROM faces fa "
            "JOIN files fi ON fi.id = fa.file_id "
            "WHERE fi.folder_id IN (" + ph + ")", vals)[0]
        ppl = store._query(
            "SELECT COUNT(DISTINCT p.id) AS n FROM people p "
            "JOIN faces fa ON fa.person_id = p.id JOIN files fi ON fi.id = fa.file_id "
            "WHERE p.name <> '' AND fi.folder_id IN (" + ph + ")", vals)[0]
        return {"faces": int(row["n"] or 0), "files": int(row["f"] or 0),
                "named_people": int(ppl["n"] or 0)}

    # ---- 写路径权限（多人 P2-b ②）----
    # 规则来自 docs/多人使用设计.md §1 的权限矩阵：
    #   · 共享库（owner_id=0）对普通用户**只读** —— 能搜能看，不能改 ✗
    #   · 普通人改自己的私有文件夹 ✓；改别人的 → 403 ✓
    #   · 建索引 / 全局设置 / 对话模型 / 备份 / 图片打标 → 只有管理员 ✓
    #   · 人物命名：只有"整组脸都在我能写的文件夹里"才让改 ✓（共享库的人名只读）
    #   · **账号体系没启用（users 表为空）→ 一律放行** ✓
    #     （设计文档 §3：首启/单人模式不能把自己锁在外面）
    def _who(request) -> dict | None:                             # noqa: ANN001
        """当前登录用户；没登录/出错都是 None。"""
        try:
            from .auth import COOKIE_NAME as _COOKIE, Auth as _Auth

            return _Auth(cfg.db_path).who(request.cookies.get(_COOKIE))
        except Exception as exc:                                  # noqa: BLE001
            log.warning("取当前用户失败：%s", exc)
            return None

    def _auth_on() -> bool:
        """账号体系是否已启用 = users 表里有没有账号。"""
        try:
            import sqlite3 as _sq

            con = _sq.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=10)
            try:
                return bool(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            finally:
                con.close()
        except Exception:                                         # noqa: BLE001
            return False                                          # 读不到 → 当没启用（老行为）

    def _need_login(request) -> dict | None:                       # noqa: ANN001
        if not _auth_on():
            return None
        u = _who(request)
        if not u:
            raise HTTPException(status_code=401, detail="请先登录再做这个操作")
        return u

    def _need_builder(request) -> dict | None:                     # noqa: ANN001
        """**建库类**操作：必须是管理员（本机也一样）✓

        和 `_need_admin_or_local` 的区别：
          · 那个管**个人数据**（备份、还原、快照、个人 API）—— 本机登录就够
          · 这个管**建库**（加/删文件夹、建索引、打标、全局设置）——
            本机也必须是管理员，否则普通用户能把库搞乱 ✗

        项目主 2026-10-22 要"通用版"：同一个安装包，
        管理员号登录 = 管理员版，普通用户号登录 = 受限版。
        """
        if not _auth_on():
            return None
        u = _who(request)
        if not u:
            raise HTTPException(status_code=401, detail="请先登录再做这个操作")
        # ★ 2026-10-22 项目主澄清：**本机功能全开** —— 这台电脑是他自己的，
        #   建库/索引/打标他自己说了算（普通用户也该能用）✓
        #   真正的界限是"能不能操作服务器"：服务器上的管理动作必须管理员 ✗
        if _can_index_here():
            return u                       # 本机：登录即可（不管角色）
        if u.get("role") != "admin":
            raise HTTPException(
                status_code=403,
                detail="服务器上的管理操作只有管理员能做。"
                       "本机上的建库/打标不受限制（这台是你自己的电脑）✓")
        return u

    def _need_admin_or_local(request) -> dict | None:              # noqa: ANN001
        """管理类操作：**本机登录即可**，服务器上必须管理员 ✓

        ★ 2026-10-20 项目主明确了两件事，合起来就是这个函数：
          "普通用户应该能备份还原他本机的，只是不能操作服务器"
          "成员个人版应该在他本机上能建库索引打标及设置个人API等"

        所以判据是「这台机器是不是他自己那台」，不是「他的角色是什么」：
          · 本机（建库机）→ 登录就够：备份、还原、拍快照、打标、索引、设 API 全放行
            （他自己电脑上的库，当然他自己说了算 ✓）
          · 服务器 → 必须管理员（否则 A 就能把全站的备份/快照/打标搞乱 ✗）
        """
        if not _auth_on():
            return None
        u = _who(request)
        if not u:
            raise HTTPException(status_code=401, detail="请先登录再做这个操作")
        if _can_index_here():
            return u                       # 本机：个人版，登录即可
        if u.get("role") != "admin":
            raise HTTPException(
                status_code=403,
                detail="这台是服务器：备份 / 还原 / 打标 / 索引这类操作只有管理员能做。"
                       "想管理自己的数据，请在自己电脑上打开本机版 ✓")
        return u

    def _need_admin(request) -> dict | None:                       # noqa: ANN001
        if not _auth_on():
            return None
        u = _who(request)
        if not u:
            raise HTTPException(status_code=401, detail="请先登录（这个操作只有管理员能做）")
        if u.get("role") != "admin":
            raise HTTPException(status_code=403, detail="这个操作只有管理员能做")
        return u

    def _writable(request) -> set[int] | None:                     # noqa: ANN001
        """我能改的文件夹；None = 不限制（管理员 / 没启用账号体系）。"""
        if not _auth_on():
            return None
        # ★ 本机 = 自己的机器：所有文件夹都能改（和服务器管理员一样）✓
        if _local_open(cfg.local_strict):
            return None
        u = _who(request)
        if u and u.get("role") == "admin":
            return None
        from .multiuser import writable_folder_ids as _wf

        ids = _wf(cfg.db_path, u)
        return None if ids is None else set(int(x) for x in ids)

    def _need_folder_write(request, folder_id: int) -> None:        # noqa: ANN001
        w = _writable(request)
        if w is None:
            return
        if int(folder_id) not in w:
            if not _who(request):
                raise HTTPException(status_code=401, detail="请先登录再做这个操作")
            raise HTTPException(
                status_code=403,
                detail="这个文件夹不是你名下的（共享库对普通用户只读），改不了")

    def _folder_ids_error(request, ids) -> None:                   # noqa: ANN001
        """批量操作：有一个不归我就整单拒掉（宁严不宽 ✓）。"""
        w = _writable(request)
        if w is None:
            return
        bad = [int(i) for i in ids if int(i) not in w]
        if bad:
            if not _who(request):
                raise HTTPException(status_code=401, detail="请先登录再做这个操作")
            raise HTTPException(status_code=403,
                                detail=f"这些文件夹不归你（共享库只读）：{bad}")

    def _person_folders(person_id: int) -> set[int]:
        return {int(r["folder_id"] or 0) for r in store._query(
            "SELECT DISTINCT f.folder_id FROM faces fa JOIN files f ON f.id=fa.file_id "
            "WHERE fa.person_id=?", (int(person_id),))}

    def _need_people_write(request, *person_ids) -> None:          # noqa: ANN001
        """人物级的写（改名 / 换封面 / 整组处理 / 合并）。

        为什么要求**整组都在我能写的文件夹里**：`people` 是全局一份，改名字
        会同时改到共享库里的那个人 ✗（设计文档：共享库的人名只读）。
        所以只要这个人有**一张**脸在共享库或别人的文件夹里，就不让改 ✓。
        """
        w = _writable(request)
        if w is None:
            return
        for pid in person_ids:
            folders = _person_folders(int(pid))
            if not folders:
                raise HTTPException(status_code=404, detail="没有这个人物")
            outside = sorted(folders - w)
            if outside:
                if not _who(request):
                    raise HTTPException(status_code=401, detail="请先登录再做这个操作")
                raise HTTPException(
                    status_code=403,
                    detail="这个人物里混着共享库（或别人文件夹）里的照片，"
                           "人名和封面只能在完全属于你的文件夹里改")

    def _need_face_write(request, face_id: int) -> None:           # noqa: ANN001
        w = _writable(request)
        if w is None:
            return
        rows = store._query(
            "SELECT f.folder_id FROM faces fa JOIN files f ON f.id=fa.file_id "
            "WHERE fa.id=?", (int(face_id),))
        if not rows:
            raise HTTPException(status_code=404, detail="没有这张人脸")
        if int(rows[0]["folder_id"] or 0) not in w:
            if not _who(request):
                raise HTTPException(status_code=401, detail="请先登录再做这个操作")
            raise HTTPException(status_code=403,
                                detail="这张脸所在的文件夹不归你，改不了")

    def _folder_of_path(real: Path) -> int | None:
        """这个路径落在哪个（匹配得最深的）已索引文件夹里；都不在 → None。"""
        best, best_len = None, -1
        for f in store.list_folders(stats=False):
            try:
                # ★ 用**这台机器视角**的根来比（服务器上是 /mnt/...，本机是盘符）✓
                root = safe_resolve(_folder_root(f))
                safe_resolve(real).relative_to(root)
            except (ValueError, OSError):
                continue
            if len(str(root)) > best_len:
                best, best_len = int(f["id"]), len(str(root))
        return best

    def _to_this_host(raw: str) -> Path:
        """把"库里的路径"换成**这台机器上真的能打开**的路径 ✓

        项目主 2026-10-20（服务器实测抓到的）：
          库里存 `\\\\192.168.1.20\\Shared\\...pdf`，
          服务器上这个路径不存在 ✗ → 预览直接 403「不在已授权的文件夹里」✗
          换成本机的视角（/mnt/nas_shared/...）就通了 ✓

        规则：原路径存在就用它；不存在且这台是服务器 → 试自动转换 ✓
        """
        p = Path(str(raw))
        try:
            if p.exists():
                return p
        except Exception:                                        # noqa: BLE001
            pass
        try:
            from . import serverpath as _sp
            if _sp.is_server():
                alt = _sp.suggest_server_path(str(raw))
                if alt and Path(alt).exists():
                    return Path(alt)
        except Exception:                                        # noqa: BLE001
            pass
        return p

    def _need_path_read(request, raw: str) -> None:               # noqa: ANN001
        """打开/定位一个**绝对路径**时，检查它是否落在别人的私有文件夹里。

        路径对不上任何已索引文件夹 → 交回原逻辑处理（避免把正常打开弄坏 ✗）。
        """
        vis = _visible_folders(request)
        if vis is None:
            return
        try:
            fid = _folder_of_path(_to_this_host(raw))
        except Exception:                                         # noqa: BLE001
            return
        if fid is not None and fid not in vis:
            raise HTTPException(status_code=403,
                                detail="这个路径在别人的私有文件夹里，看不到")

    def _folder_root(folder) -> str:
        """**这台机器**上该去哪个目录访问这个文件夹 ✓

        项目主 2026-10-20："服务器接上"
          · 本机（建库机）→ local_alias 优先（本机也可自定义同步目录）
          · 服务器        → server_path 优先（库里 path 是 Windows 视角，
                            Linux 上 //... 打不开 ✗）
        库里的 `path` 始终不变（它是 files.path 的前缀、权威键）✓
        """
        from . import serverpath as _sp
        try:
            p = store.folder_paths(int(folder["id"]))
        except Exception:                                        # noqa: BLE001
            p = {"path": str(folder["path"] or ""), "local": "", "server": ""}
        return _sp.pick_root(p["path"], p.get("local", ""), p.get("server", ""))

    def _allowed_roots() -> list[Path]:
        roots = []
        for folder in store.list_folders():
            try:
                roots.append(safe_resolve(_folder_root(folder)))
            except OSError:
                continue
        return roots

    def _is_inside_indexed(path: Path) -> bool:
        try:
            target = safe_resolve(path)
        except OSError:
            return False
        for root in _allowed_roots():
            try:
                target.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    # ---------------------------------------------------------- 页面
    @router.get("/search", include_in_schema=False)
    async def search_page():
        page = static_dir / "search.html"
        if not page.exists():
            return JSONResponse({"detail": "search.html 缺失"}, status_code=500)
        # 页面绝对不能缓存：每次改完前端都让浏览器拿最新的，
        # 省得用户困惑「我明明改了怎么没生效」，被迫去按 Ctrl+Shift+R。
        return FileResponse(
            page, media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                     "Pragma": "no-cache", "Expires": "0"})

    # ---------------------------------------------------------- 目录浏览
    @router.get("/api/fs/roots")
    async def fs_roots(request: Request):
        # 浏览磁盘只为了"挑一个文件夹加进来"（那一步本来就要登录）✓；
        # 匿名不给逛宿主机目录 ✗（私有文件夹的路径会因此暴露）
        _need_login(request)
        if sys.platform == "win32":
            drives = []
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                root = f"{letter}:\\"
                if _safe_isdir(root):
                    drives.append({"name": f"{letter}:", "path": root})
            return {"roots": drives}
        return {"roots": [{"name": "/", "path": "/"}]}

    @router.get("/api/fs/list")
    def fs_list(request: Request,
                path: str = Query("", description="要浏览的目录，留空返回磁盘列表"),
                folder_id: int = Query(0, description="给了就按这个文件夹的排除规则过滤子目录")):
        """列目录（一层）。

        ⚠ 2026-10-18：改成同步 def ✓ —— NAS 上列一层也可能要好几秒，
          async def 里跑同步 scandir 会把事件循环占住（和预览排除同一个坑）✗

        ⚠ 2026-10-18 之二：加了 `folder_id` ✓ —— 项目主反馈
          "我选择了排除，怎么子目录里还有？"
          原因：这里以前**只看全局 ignore_dirs** ✗，完全不认文件夹里填的排除规则，
          所以写了 `.syncing_db` 排除，子目录归类列表里照样列出来 ✗
          现在给了 folder_id 就按那个文件夹的规则一起过滤 ✓
        """
        _need_login(request)
        if not path:
            if sys.platform == "win32":
                dirs = [{"name": f"{letter}:", "path": f"{letter}:\\"}
                        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ" if _safe_isdir(f"{letter}:\\")]
            else:
                dirs = [{"name": "/", "path": "/"}]
            return {"path": "", "parent": None, "dirs": dirs}

        target = Path(path)
        if not _safe_isdir(str(target)):
            raise HTTPException(status_code=404, detail=f"目录不存在或无法访问：{path}")
        # 先算出"已索引的文件夹"集合（规范化成大小写不敏感的绝对路径 ✓）
        # ⚠ 2026-10-17 报错修复：原来对**每个子目录**都调 `Path.resolve()` ✗，
        #   而它会对条目做 realpath —— 碰到**连不上的网络盘**（凭据失效的映射盘，
        #   实测 Z:\Pro-Calibre）直接抛 `OSError: WinError 1326 用户名或密码不正确` ✗，
        #   整个目录列表变成 500「Internal Server Error」✗。
        #   现在改成纯字符串规范化（`normcase(abspath)`）：不发 IO ✓ 不碰网络盘 ✓ 也快得多 ✓。
        try:
            indexed = {os.path.normcase(os.path.abspath(str(f["path"])))
                       for f in store.list_folders()}
        except Exception as exc:                                  # noqa: BLE001
            log.warning("读取已索引文件夹失败（列表照常返回）：%s", exc)
            indexed = set()
        dirs = []
        # 给了 folder_id 就取它的排除规则（用于过滤子目录列表）✓
        rule_cfg = None
        rule_root = None
        if int(folder_id or 0) > 0:
            try:
                frow = store.get_folder(int(folder_id))
                if frow is not None:
                    rule_root = Path(frow["path"])
                    rule_cfg = FolderConfig(cfg, frow.get("config"))
            except Exception as exc:                              # noqa: BLE001
                log.debug("取文件夹排除规则失败（照常列目录）：%s", exc)
        try:
            for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                name = entry.name
                if name.startswith("$") or name.lower() in cfg.ignore_dirs:
                    continue
                # ★ 还要按**这个文件夹自己的排除规则**过滤 ✓
                #   （否则用户写了排除，子目录归类列表里照样出现 ✗）
                if rule_cfg is not None:
                    try:
                        if rule_cfg.excluded(Path(entry.path), rule_root, True):
                            continue
                    except Exception:                             # noqa: BLE001
                        pass
                try:
                    key = os.path.normcase(os.path.abspath(entry.path))
                    is_indexed = key in indexed
                except OSError:                                   # noqa: BLE001
                    is_indexed = False                            # 算不出来就当没索引 ✓
                dirs.append({
                    "name": name,
                    "path": entry.path,
                    "indexed": is_indexed,
                })
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"没有权限读取：{path}") from exc
        parent = str(target.parent) if target.parent != target else None
        return {"path": str(target), "parent": parent, "dirs": dirs}

    # ---------------------------------------------------------- 文件夹管理
    @router.get("/api/folder-categories")
    async def get_folder_categories(request: Request):
        """类别表：内置 6 类（含被改过的默认口径）+ 自定义类别 ✓（2026-10-18 A+B）

        `mode_switches`：每个「索引模式」预设对应的开关组合 ✓
        给界面做**联动**用 —— 选了模式，下面的开关自动跟着变 ✓
        （项目主 2026-10-18："选了索引模式，下面相应开关应该联动，这样更加直观"）
        """
        _need_admin_or_local(request)
        switches = {}
        for mid, preset in FC.MODE_PRESETS.items():
            one = {}
            for key, value in preset.items():
                one[key] = str(value).strip().lower() in ("1", "yes", "true", "on", "y")
            switches[mid] = one
        return {
            "categories": FC.categories_info(),
            "custom": FC.get_custom_data(),
            "builtin": FC.builtin_categories(),
            "modes": [{"id": k, "label": v} for k, v in FC.MODE_LABELS.items()],
            "mode_switches": switches,
            "switch_keys": ["index_text", "index_images", "index_audio", "index_video"],
            "path": FC._ACTIVE_PATH,                              # noqa: SLF001
        }

    @router.put("/api/folder-categories")
    async def put_folder_categories(request: Request, payload: dict = Body(...)):
        """保存类别表：`{"overrides": {...}, "custom": [...]}` ✓

        权限：**管理员**才能改 ✓ —— 这是"这台机器上大家新增文件夹时的默认口径"，
        普通用户改会影响到别人 ✗（他们仍然可以在单个文件夹的「配置」里自己调 ✓）。
        """
        u = _need_login(request)
        if u and u.get("role") != "admin" and not _can_index_here():
            raise HTTPException(status_code=403,
                                detail="类别默认值是管理员设置的；你可以单独调整某个文件夹的配置 ✓")
        data = {"overrides": payload.get("overrides") or {},
                "custom": payload.get("custom") or []}
        try:
            FC.save_categories(data)
        except Exception as exc:                                 # noqa: BLE001
            raise HTTPException(status_code=500,
                                detail=f"保存类别表失败：{exc}") from exc
        cats = FC.categories_info()
        return {"ok": True, "count": len(cats), "categories": cats,
                "custom": FC.get_custom_data(),
                "message": f"已保存：{len(cats)} 个类别"
                           f"（自定义 {sum(1 for c in cats if not c.get('builtin'))} 个）"}

    @router.get("/api/folders")
    async def list_folders(request: Request):
        """文件夹**管理**列表。

        2026-10-16 项目主纠正：这里只列**自己的**文件夹 ✓ ——
        普通用户不该在"文件夹"页看到管理员建的文件夹（那一行全是
        「移除 / 配置 / 增量更新 / 全部重建」这种管理动作 ✗，不是给用户看的）。
        但**共享出来的内容照样能搜、能问答、能预览** ✓（那是"可读范围"的事，
        见 `_visible_folders`）—— 两件事分开：**能不能读 ≠ 列不列在管理页** ✓。
        """
        vis = _visible_folders(request)                       # 可读范围
        # 这台机器 + 当前登录的人 → 前端该显示"个人版 / 管理员版 / 成员版" ✓
        _vm = _view_mode(str((_who(request) or {}).get("role") or ""),
                         can_index=_can_index_here())
        # 「自己拥有的」= 可写范围（管理员 None=不限制；匿名=空 ✓ 语义正好吻合）
        try:
            from .multiuser import writable_folder_ids as _writable

            # ★ 2026-10-21：本机（建库机）不限制 —— 本机的普通用户和管理员一样
            #   能看能改本机所有文件夹（项目主："只差能不能管服务器"）✓
            wr = None if _local_open(cfg.local_strict) else _writable(cfg.db_path, _who(request))
            wset = None if wr is None else {int(x) for x in wr}
        except Exception as exc:                                  # noqa: BLE001
            log.warning("取可写文件夹失败，按「只看自己的」处理：%s", exc)
            wset = set()
        folders = [f for f in store.list_folders()
                   if (vis is None or int(f["id"]) in vis)
                   and (wset is None or int(f["id"]) in wset)]
        for folder in folders:
            fcfg = FolderConfig(cfg, folder.get("config"), engine)
            folder["summary"] = fcfg.summary()
            # 用途类别（2026-10-17）：老数据是空串 → 显示「未分类」✓ 行为不变 ✓
            _cat = str(folder.get("category") or "")
            folder["category"] = _cat
            folder["category_label"] = FC.category_label(_cat)
            folder["category_icon"] = (FC.CATEGORIES.get(_cat) or {}).get("icon", "•")
            # 管理页里能看到的就是自己的 → 都能改 ✓（留着这个字段让前端逻辑统一）
            folder["writable"] = True
            # ★ 2026-10-20：🔒 隐私文件夹（内容永不出机器）—— 界面要能显示和切换 ✓
            folder["privacy"] = bool(int(folder.get("privacy") or 0))
            # ★ 2026-10-27：分清楚"这路径服务器上本来就没有"和"网络盘掉线了" ——
            #   服务器是 Linux，建库机上的 `\\NAS\share` / `D:\x` 在它那儿当然不存在 ✗
            #   原来一律显示"⚠ 目录不存在"，看着像故障（项目主就误会过）✓
            _p = str(folder.get("path") or "")
            _win = bool(re.match(r"^[A-Za-z]:[\\/]", _p)) or _p.startswith("\\\\")
            folder["path_kind"] = "windows" if _win else "posix"
            if not folder.get("exists"):
                folder["exists_reason"] = ("windows_path"
                                           if (_win and not _can_index_here())
                                           else "missing")
        return {
            "folders": folders,
            # ★ 2026-10-21 项目主："用户登陆，这里不应该显示整站全局信息，
            #   只能显示用户文件信息" → 统计按**他能读的文件夹**算 ✓
            #   （管理员 vis 是 None → 仍是全站；看不到任何东西的人给全 0，不退化成全站 ✗）
            "stats": store.stats(folder_ids=vis),            "schema": FC.KEYS,
            "modes": [{"id": key, "label": label} for key, label in FC.MODE_LABELS.items()],
            # 类别清单也从后端给 ✓（定义只在 app/folderconf.py 一处 ✓）
            "categories": FC.categories_info(),
            "default_category": FC.DEFAULT_CATEGORY,
            "default_dim": cfg.index_dim,
            # ★ 2026-10-20 项目主："文件夹自定义映射服务器地址功能只需添加到本机版即可，
            #   服务器版上不用" → 前端靠这个标志决定要不要显示「🔗 路径」按钮 ✓
            #   判据和索引权限同一个（见 _can_index_here）✓
            "can_index_here": _can_index_here(),
            "is_builder": _can_index_here(),
            # ★ 2026-10-20 项目主："本地版是希望个人专用（应该有管理员版可以统管全局，
            #   与个人版区别）" → 这里算出"这台机器现在是什么形态"，前端据此显示不同界面 ✓
            #   personal = 个人版（本机、就我自己用）：一切以"我"为中心，不显示多人管理
            #   admin    = 管理员版（服务器/多人）：统管全局（用户、共享、索引、打标、备份）
            #   member   = 成员版（服务器上的普通用户）：只看自己的 + 别人共享的
            #   角色必须**在这里**取（`_who` 是闭包函数，模块级调不到，见 _view_mode 的注释）
            "view_mode": _vm,
            "view_mode_label": _VIEW_LABELS.get(_vm, ""),
            # ★ 2026-10-21 项目主："服务器用户图片打标用API可行，服务器本地模型也变灰"
            #   本机（建库机）才有 GPU + 本地 VL；服务器上只能走外部 API ✓
            "local_vl_ok": _can_index_here(),
        }

    # ---------- 目录可用性重查（2026-10-27 项目主：NAS 重启滞后 → 显示"目录不存在"）
    def _mount_of(path: str) -> str:
        """这个路径落在哪个挂载点上（Linux 读 /proc/mounts；其它系统返回空）。"""
        try:
            best = ""
            with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    mp = parts[1].replace("\\040", " ")
                    if mp and (path == mp or path.startswith(mp.rstrip("/") + "/")):
                        if len(mp) > len(best):
                            best = mp
            return best
        except Exception:                                        # noqa: BLE001
            return ""

    @router.get("/api/folders/recheck")
    async def folders_recheck(request: Request):
        """立刻重查每个文件夹：在不在、挂载点还在不在。

        项目主 2026-10-27："NAS 重启滞后，导致网络连接 NAS 硬盘显示不存在" ——
        界面原来只能靠 30 秒轮询，且分不清"没挂载"和"目录真的没了"。
        这个接口**现算**，给「重新检查目录」按钮用；顺便把挂载点一起返回 ✓
        """
        _need_login(request)
        vis = _visible_folders(request)
        # ⚠ 这里**不能**用 store.list_folders()：它会把 19 个路径挨个 stat 一遍，
        #   网络盘慢的时候光这一步就十几秒（"重新检查"按钮直接卡住）✗
        #   只取 id/path，真正的检查交给下面的并行探测 ✓
        rows = [dict(r) for r in store._query(                     # noqa: SLF001
            "SELECT id, path FROM folders ORDER BY added_at")
            if vis is None or int(r["id"]) in vis]

        # ⚠ 网络盘上 os.path.isdir 一次可能卡好几秒（19 个串起来能到十几秒），
        #   所以**并行查 + 总超时 6 秒**：超时的标成"还没探到"，不拖住按钮 ✓
        def _probe(f: dict) -> dict:
            path = str(f.get("path") or "")
            try:
                ok = os.path.isdir(path)
            except OSError:
                ok = False
            mp = _mount_of(path)
            mounted = True
            if mp:
                try:
                    mounted = os.path.ismount(mp) or os.path.isdir(mp)
                except OSError:
                    mounted = False
            _win = bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")
            reason = ""
            if not ok:
                if _win and not _can_index_here():
                    reason = "windows_path"
                elif mp and not mounted:
                    reason = "not_mounted"
                else:
                    reason = "missing"
            return {"id": int(f["id"]), "path": path, "exists": bool(ok),
                    "mount": mp, "mounted": bool(mounted), "reason": reason}

        out: list[dict] = []
        try:
            from concurrent.futures import ThreadPoolExecutor, wait

            pool = ThreadPoolExecutor(max_workers=8)
            futs = [pool.submit(_probe, f) for f in rows]
            # ⚠ 不能用 `with`：它退出时会等所有线程跑完，网络盘卡住照样拖十几秒 ✗
            done, _pending = wait(futs, timeout=4)
            for fut in done:
                try:
                    out.append(fut.result())
                except Exception:                                # noqa: BLE001
                    pass
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:                                 # noqa: BLE001
            log.warning("并行检查目录失败，退回串行：%s", exc)
            out = [_probe(f) for f in rows]

        done_ids = {x["id"] for x in out}
        for f in rows:                       # 没探到的（超时）如实标出来
            if int(f["id"]) not in done_ids:
                out.append({"id": int(f["id"]), "path": str(f.get("path") or ""),
                            "exists": None, "mount": "", "mounted": None,
                            "reason": "timeout"})
        out.sort(key=lambda x: x["id"])
        alive = sum(1 for x in out if x["exists"])
        return {"ok": True, "total": len(out), "alive": alive,
                "dead": [x for x in out if x["exists"] is not True],
                "folders": out}

    def _prepare_override(payload: dict) -> dict:
        """把请求里的配置整理成一份文件夹配置。

        顺序很关键（2026-10-17 加类别时定的）：
          ① 先套**用途类别**的经济默认 ✓（payload["category"]）
          ② 再让**用户显式传的** config 覆盖它 ✓（用户想调就调 ✓）
          ③ 最后如果显式给了 mode，以 mode 为准 ✓
        这样"选个类别就有合理默认"和"我就要自定义"两件事都不冲突 ✓。
        """
        base = FC.apply_category({}, payload.get("category"))
        explicit = FC.normalize(payload.get("config") or {})
        override = FC.normalize({**base, **explicit})
        mode = payload.get("mode")
        if mode:
            override = FC.apply_mode(override, str(mode).strip().lower())
        return FC.normalize(override)

    def _conflicts(target: Path, folders: list[dict]) -> str | None:
        for existing in folders:
            root = Path(existing["path"])
            if str(target) == str(root):
                return f"已经在索引里：{existing['path']}"
            try:
                target.relative_to(root)
                return f"已被「{existing['path']}」覆盖，无需重复添加"
            except ValueError:
                pass
            try:
                root.relative_to(target)
                return f"与已添加的「{existing['path']}」重叠，请先移除那个"
            except ValueError:
                pass
        return None

    @router.post("/api/folders")
    async def add_folders(request: Request, payload: dict = Body(...)):
        """支持一次添加多个文件夹：paths 数组（兼容旧的单个 path）。

        多人规则（设计文档 §1 + 项目主 2026-10-16 补充）：
          · 必须登录 ✓（匿名不能往库里加东西）
          · 普通人加的文件夹归**他自己**（owner_id=自己）✓
          · 索引：
              · **本机（建库机）** → 用户自己就能跑 ✓（不用等批准 ✓）
              · **服务器**        → 记 `pending`，等管理员批准 ✓
                （索引以建库机为权威，服务器上建了也会被覆盖 ✗）
          · 管理员照旧：owner_id=0 + 立刻建索引 ✓
        """
        u = _need_builder(request)
        # 本机（建库机）= 自己的机器，和服务器管理员同权 ✓
        is_admin = _can_index_here() or (not u) or u.get("role") == "admin"
        can_index = _can_index_here()

        raw_paths = payload.get("paths")
        if not raw_paths:
            single = payload.get("path")
            raw_paths = [single] if single else []
        if not raw_paths:
            raise HTTPException(status_code=400, detail="请至少选择一个文件夹")

        override = _prepare_override(payload)
        # 用途类别（2026-10-17 项目主要求）：加文件夹时选一个，就用它的经济默认 ✓
        category = str(payload.get("category") or "").strip().lower()
        if category and category not in FC.CATEGORIES:
            raise HTTPException(status_code=400, detail=f"未知的文件夹类别：{category}")
        scan = payload.get("scan", True)
        added: list[dict] = []
        skipped: list[dict] = []

        for raw in raw_paths:
            raw = str(raw).strip().strip('"')
            if not raw:
                continue
            target = Path(raw).expanduser()
            if not _safe_isdir(str(target)):
                skipped.append({"path": raw, "reason": "不是有效文件夹"})
                continue
            target = safe_resolve(target)
            problem = _conflicts(target, store.list_folders(stats=False))
            if problem:
                skipped.append({"path": str(target), "reason": problem})
                continue
            folder_id = store.add_folder(str(target), override, category)
            if not is_admin and u:
                # 归属 + 状态：owner_id 不是 0 才不会混进共享库 ✓
                # 本机能自己建索引 → 直接 ready ✓；服务器上等管理员批准 → pending ✓
                store.set_folder_owner(folder_id, int(u["id"]),
                                       "ready" if can_index else "pending")
            added.append({"id": folder_id, "path": str(target),
                          "category": category, "category_label": FC.category_label(category)})

        if not is_admin and not can_index:
            scan = False                                          # 服务器上要管理员批准 ✓

        if added and scan:
            indexer.start([item["id"] for item in added])

        return {
            "added": added,
            "skipped": skipped,
            "count": len(added),
            "mode": override.get("mode", FC.MODE_INHERIT),
            "mode_label": FC.describe(override),
            "pending_approval": bool(added and not is_admin),
            "message": (
                f"已添加 {len(added)} 个文件夹，正在后台逐个建立索引" if added and scan
                else (f"已提交 {len(added)} 个文件夹，等管理员批准后才会建索引"
                      if added else "没有新增文件夹")),
        }

    @router.put("/api/folders/{folder_id}/config")
    async def set_folder_config(request: Request, folder_id: int, payload: dict = Body(...)):
        """保存文件夹配置。

        ⚠ 2026-10-17 事故与修复：这里原来是**整体替换** config ✗ ——
        我用 `PUT {"config": {"fast_scan": false}}` 只为验证新开关能不能收下，
        结果把项目主在这个文件夹上设的 `use_kb`（作为知识库）**一起抹掉了** ✗✗
        （快照对比铁证：改前 `{… "use_kb": true …}`，改后只剩 `{"fast_scan": false}`）。
        所以改成**合并**语义（patch ✓）：
          · 提交里出现的键 → 覆盖 ✓
          · 没出现的键 → **保留** ✓（前端提交的是一整份，行为不变 ✓）
          · 想让某个键回到默认 → 显式传 `null`（就会把它删掉 ✓）
        """
        folder = store.get_folder(folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        _need_folder_write(request, folder_id)

        before = FolderConfig(cfg, folder.get("config"), engine).index_dim
        override = _prepare_override(payload)
        # 默认**整体替换**（前端提交的是全部控件的值，且"清空"就是靠不提交/空值 ✓
        # —— 改成合并会让"清空排除规则"失效 ✗，所以语义不动）。
        # 但要**显式**支持部分修改：传 `merge: true` 就只改提到的键 ✓
        merge = bool(payload.get("merge"))
        merged = (_merge_config(folder.get("config"), override, payload.get("config") or {})
                  if merge else override)
        store.set_folder_config(folder_id, merged)
        after = FolderConfig(cfg, merged, engine).index_dim
        # ★ 防呆：整体替换时，把"这次被清掉的原有键"回显出来 ✓
        # 2026-10-17 事故：脚本里 `PUT {"config": {"fast_scan": false}}` 把项目主设的
        # `use_kb` 悄悄抹掉了 ✗，谁都没发现。有了这个字段，一眼就能看见 ✓
        dropped = sorted(set(_config_keys(folder.get("config"))) - set(merged.keys()))
        if dropped:
            log.warning("文件夹 #%s 的配置被整体替换，清掉了这些键：%s",
                        folder_id, dropped)
        need_reindex = before != after
        message = "已保存"
        if need_reindex:
            message = (f"向量维度从 {before} 改成 {after}，该文件夹需要重新建立索引")
        if dropped:
            message += f"（注意：这次清掉了 {', '.join(dropped)}）"
        return {
            "ok": True, "id": folder_id,
            "config": merged,
            "label": FC.describe(merged),
            "dropped_keys": dropped,
            "need_reindex": need_reindex,
            "message": message,
        }

    def _folder_dim(fid: int) -> int | None:
        """这个文件夹**已有片段实际用的维度**（取条数最多的那个）。

        项目主 2026-10-18：改「用途类别」时用它**沿用现有维度** ✓ ——
        否则"只想换个用途说法"就会因为类别默认是别的维度而**被迫整库重建** ✗
        （#36 里历史上有 256/512/768 三种，所以取最多的那个当代表 ✓）
        """
        try:
            rows = store._query(                              # noqa: SLF001
                "SELECT ch.dim AS d, COUNT(*) AS n FROM chunks ch "
                "JOIN files f ON f.id = ch.file_id "
                "WHERE f.folder_id = ? AND ch.dim > 0 "
                "GROUP BY ch.dim ORDER BY n DESC LIMIT 1", (int(fid),))
            if rows:
                value = int(rows[0]["d"] or 0)
                return value or None
        except Exception as exc:                                 # noqa: BLE001
            log.debug("查文件夹 %s 的现有维度失败：%s", fid, exc)
        return None

    @router.get("/api/folders/errors/summary")
    async def folders_errors_summary(request: Request):
        """每个文件夹的「异常」到底是什么原因（给界面做悬停提示用）。

        项目主 2026-10-18："实在有异常，给个鼠标放上去的提示说明，让我知道什么原因"
        → 按 note 归类，每类给数量 + 一条样例文件名 ✓
        """
        _need_login(request)
        vis = _visible_folders(request)
        rows = store._query(                                  # noqa: SLF001
            "SELECT f.folder_id AS fid, f.note AS note, COUNT(*) AS n, "
            "MIN(f.name) AS sample FROM files f WHERE f.status='error' "
            "GROUP BY f.folder_id, f.note ORDER BY f.folder_id, n DESC")
        out: dict = {}
        for r in rows:
            fid = int(r["fid"])
            if vis is not None and fid not in vis:
                continue
            note = str(r["note"] or "（没有说明）")
            # 太长的原因截断（界面要在一行里显示）
            if len(note) > 80:
                note = note[:80] + "…"
            out.setdefault(fid, []).append(
                {"note": note, "n": int(r["n"]), "sample": str(r["sample"] or "")})
        return {"ok": True, "by_folder": out,
                "hint": ("异常 = 这个文件在读内容/解码时失败了（图片格式不支持、文件损坏、"
                         "PDF 提取不出文字等）。它不影响其它文件；重建一次索引会按新规则重新判定。")}


    @router.get("/api/folders/{folder_id}/errors")
    async def folder_errors(request: Request, folder_id: int,
                            limit: int = Query(500)):
        """这个文件夹里**具体哪些文件**出问题了（项目主 2026-10-18：
        "最好提示说明哪个文件出问题，我好针对它做出修改"）。

        返回完整路径 ✓（方便直接去改/删）+ 原因 + 大小 + 时间 ✓
        """
        _need_login(request)
        vis = _visible_folders(request)
        if vis is not None and int(folder_id) not in vis:
            raise HTTPException(status_code=403, detail="这个文件夹对你是私有的，看不到")
        folder = store.get_folder(folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        limit = max(1, min(int(limit or 500), 2000))
        rows = store._query(                                    # noqa: SLF001
            "SELECT path, name, note, kind, size, mtime FROM files "
            "WHERE folder_id = ? AND status = 'error' "
            "ORDER BY path LIMIT ?", (int(folder_id), limit))
        total = store._query(                                   # noqa: SLF001
            "SELECT COUNT(*) AS n FROM files WHERE folder_id = ? AND status = 'error'",
            (int(folder_id),))
        items = [{
            "path": str(r["path"] or ""),
            "name": str(r["name"] or ""),
            "note": str(r["note"] or ""),
            "kind": str(r["kind"] or ""),
            "size": int(r["size"] or 0),
            "mtime": float(r["mtime"] or 0),
        } for r in rows]
        return {
            "ok": True,
            "folder_id": int(folder_id),
            "root": str(folder["path"]),
            "total": int((total[0]["n"] if total else 0)),
            "returned": len(items),
            "items": items,
            "hint": ("这些文件在读内容/解码时失败了。常见处理：图片转成 jpg/png、"
                     "损坏的文件删掉或重新下载、扫描版 PDF 先做 OCR。"
                     "处理完点「▶ 开始」重扫即可（已好的会恢复正常）。"),
        }


    @router.put("/api/folders/{folder_id}/category")
    async def set_folder_category(request: Request, folder_id: int,
                                  payload: dict = Body(...)):
        """重新分类（项目主要求：可以设置重新分类 ✓）。

        只套用这个类别的**经济默认**（mode + 类别声明的开关）✓；
        用户自己调过的其它项（排除规则、分块长度、快速扫描…）**原样保留** ✓
        （否则改个类别就把调好的参数冲掉了 ✗）。
        返回 `changed`（到底改了哪几项）+ `need_reindex`，界面上要提示清楚 ✓。
        """
        folder = store.get_folder(folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        _need_folder_write(request, folder_id)

        category = str(payload.get("category") or "").strip().lower()
        if category and category not in FC.CATEGORIES:
            raise HTTPException(status_code=400, detail=f"未知的文件夹类别：{category}")

        old_cfg = folder.get("config") or {}
        before = FolderConfig(cfg, old_cfg, engine).summary()
        # ★ 沿用这个文件夹现有的向量维度 ✓（已经有索引的话，改用途不该逼你重建）
        existing_dim = _folder_dim(folder_id)
        merged = FC.apply_category(old_cfg, category, keep_dim=existing_dim)
        store.set_folder_config(folder_id, merged)
        store.set_folder_category(folder_id, category)
        after = FolderConfig(cfg, merged, engine).summary()

        changed = sorted(k for k in after if before.get(k) != after.get(k))
        # ★ 透明：把"类别默认**改掉了你原来显式设过的**键"回显出来 ✓
        # （2026-10-17：photo 类默认 use_kb=False，会把用户手动勾的"作为知识库"改掉。
        #   这是对的——照片库当知识库只会拖慢检索——但必须让人**看得见** ✓）
        cat_conf = (FC.CATEGORIES.get(category) or {}).get("config") or {}
        overridden = [
            {"key": k, "from": old_cfg.get(k), "to": merged.get(k)}
            for k in sorted(cat_conf)
            if k in old_cfg and old_cfg.get(k) != merged.get(k)
        ]
        # 索引口径相关的变了才需要重建 ✓（只换标签不用重扫 ✓）
        heavy = {"mode", "index_text", "index_images", "index_audio", "index_video",
                 "index_dim", "chunk_chars", "max_chunks_per_file"}
        need_reindex = bool(heavy & set(changed))
        if need_reindex:
            message = (f"已改为「{FC.category_label(category)}」，"
                       f"扫描口径变了（{', '.join(sorted(heavy & set(changed)))}）"
                       f"—— 该文件夹需要重新建立索引")
        else:
            message = f"已改为「{FC.category_label(category)}」，索引口径不用变"
        if existing_dim and not any(k == "index_dim" for k in changed):
            message += f"；已沿用现有 {existing_dim} 维，**不用重建** ✓"
        if overridden:
            message += "；按该类默认同时调整了：" + ", ".join(
                f"{o['key']} {o['from']} → {o['to']}" for o in overridden)
        return {
            "ok": True, "id": folder_id, "category": category,
            "category_label": FC.category_label(category),
            "label": FC.describe(merged), "config": merged,
            "summary": after, "changed": changed, "overridden": overridden,
            "kept_dim": existing_dim,
            "need_reindex": need_reindex, "message": message,
        }

    @router.put("/api/folders/categories")
    async def set_folders_category(request: Request, payload: dict = Body(...)):
        """**批量**改用途归类（项目主 2026-10-18：多选了却没有统一设置的口子 ✗）。

        逐个套用该类别的经济默认（规则和单个改一样 ✓：只改口径相关的键，
        用户调过的排除规则等保留 ✓），并汇总哪些文件夹需要重建索引 ✓。
        没有权限的跳过（不整体失败 ✗），最后把跳过的原因一起返回 ✓。
        """
        _need_builder(request)
        ids = [int(x) for x in (payload.get("folder_ids") or [])]
        if not ids:
            raise HTTPException(status_code=400, detail="请至少选一个文件夹")
        category = str(payload.get("category") or "").strip().lower()
        if category and category not in FC.CATEGORIES:
            raise HTTPException(status_code=400, detail=f"未知的文件夹类别：{category}")

        heavy = {"mode", "index_text", "index_images", "index_audio", "index_video",
                 "index_dim", "chunk_chars", "max_chunks_per_file"}
        updated: list[int] = []
        skipped: list[dict] = []
        reindex: list[int] = []
        for fid in ids:
            folder = store.get_folder(fid)
            if folder is None:
                skipped.append({"id": fid, "reason": "文件夹不存在"})
                continue
            try:
                _need_folder_write(request, fid)
            except HTTPException as exc:                          # 没权限的跳过 ✓
                skipped.append({"id": fid, "reason": str(exc.detail)})
                continue
            old_cfg = folder.get("config") or {}
            before = FolderConfig(cfg, old_cfg, engine).summary()
            # ★ 每个文件夹各自沿用**它自己的**现有维度 ✓（批量时逐个查）
            merged = FC.apply_category(old_cfg, category, keep_dim=_folder_dim(fid))
            store.set_folder_config(fid, merged)
            store.set_folder_category(fid, category)
            after = FolderConfig(cfg, merged, engine).summary()
            if heavy & {k for k in after if before.get(k) != after.get(k)}:
                reindex.append(fid)
            updated.append(fid)

        msg = f"已把 {len(updated)} 个文件夹设为「{FC.category_label(category)}」"
        if skipped:
            msg += f"；{len(skipped)} 个跳过（没权限或不存在）"
        if reindex:
            msg += f"；{len(reindex)} 个需要重建索引"
        return {"ok": True, "updated": updated, "skipped": skipped,
                "need_reindex": reindex, "category": category,
                "category_label": FC.category_label(category), "message": msg}

    def _config_keys(old_json) -> list[str]:
        """现有配置里有哪些键（用来回显"被清掉了什么" ✓）"""
        import json as _json

        try:
            data = _json.loads(old_json) if old_json else {}
        except (ValueError, TypeError):
            return []
        return list(data.keys()) if isinstance(data, dict) else []

    def _merge_config(old_json, clean: dict, raw: dict) -> dict:
        """部分修改：只改这次提到的键 ✓（`merge: true` 时才走这里）。

        没提到的键**保留** ✓；显式传 null 表示"回到默认"（删掉该键 ✓）。
        默认的整体替换仍然保留 —— 前端"清空排除规则"依赖它 ✓
        （见 `set_folder_config` 的说明）。
        """
        import json as _json

        try:
            base = _json.loads(old_json) if old_json else {}
        except (ValueError, TypeError):
            base = {}
        if not isinstance(base, dict):
            base = {}
        out = dict(base)
        for key, value in (raw or {}).items():
            if key == "mode":
                continue
            if value is None:
                out.pop(key, None)
            elif key in clean:
                out[key] = clean[key]
        return out

    @router.post("/api/folders/config")
    async def set_folders_config(request: Request, payload: dict = Body(...)):
        """批量给多个文件夹套用同一个模式。"""
        ids = [int(x) for x in (payload.get("folder_ids") or [])]
        if not ids:
            raise HTTPException(status_code=400, detail="请先选择文件夹")
        _folder_ids_error(request, ids)                       # 有一个不归我就整单拒掉 ✓
        override = _prepare_override(payload)
        changed, reindex = [], []
        for folder_id in ids:
            folder = store.get_folder(folder_id)
            if folder is None:
                continue
            before = FolderConfig(cfg, folder.get("config"), engine).index_dim
            store.set_folder_config(folder_id, override)
            changed.append(folder_id)
            if before != FolderConfig(cfg, override, engine).index_dim:
                reindex.append(folder_id)
        return {"ok": True, "changed": changed, "need_reindex": reindex,
                "label": FC.describe(override),
                "message": f"已更新 {len(changed)} 个文件夹的模式"}

    @router.delete("/api/folders/{folder_id}")
    async def remove_folder(request: Request, folder_id: int):
        _need_folder_write(request, folder_id)
        store.remove_folder(folder_id)
        return {"ok": True}

    @router.post("/api/folders/remove")
    async def remove_folders(request: Request, payload: dict = Body(...)):
        ids = [int(x) for x in (payload.get("folder_ids") or [])]
        _folder_ids_error(request, ids)
        for folder_id in ids:
            store.remove_folder(folder_id)
        return {"ok": True, "removed": len(ids)}

    # ---------------------------------------------------------- 索引
    @router.post("/api/index/start")
    async def index_start(request: Request, payload: dict = Body(default={})):
        """启动索引。

        规则（项目主 2026-10-16 明确）：
          · **本机（建库机）**：登录用户都能跑 ✓ —— 用户自己加的文件夹，自己就能建索引 ✓
            （但只能动自己有写权限的文件夹；不指定就只跑自己的 ✓）
          · **服务器**：索引以建库机为权威（在这里建，下次同步会被整份覆盖 ✗），
            所以要**管理员**才能跑 ✓
        """
        u = _need_builder(request)
        # 本机（建库机）= 自己的机器，和服务器管理员同权 ✓
        is_admin = _can_index_here() or (not u) or u.get("role") == "admin"
        if not _can_index_here() and not is_admin:
            raise HTTPException(
                status_code=403,
                detail="这台是服务器：索引以「建库的那台机器」为权威，"
                       "在这里重建下次同步会被整份覆盖。要在服务器上建索引请让管理员操作；"
                       "普通用户可以在自己的电脑上打开界面，给自己的文件夹更新索引 ✓")
        folder_ids = payload.get("folder_ids") or None
        if not is_admin:
            from .multiuser import writable_folder_ids as _writable

            mine = {int(x) for x in (_writable(cfg.db_path, u) or [])}
            if folder_ids:
                bad = [int(x) for x in folder_ids if int(x) not in mine]
                if bad:
                    raise HTTPException(
                        status_code=403,
                        detail=f"这些文件夹不归你管，不能建索引：{bad}")
            else:
                if not mine:
                    raise HTTPException(
                        status_code=403,
                        detail="你名下还没有文件夹。先在「文件夹」页添加一个 ✓")
                folder_ids = sorted(mine)
        if engine.model is None:
            raise HTTPException(status_code=503, detail="模型还没加载完，请稍候")
        force = bool(payload.get("force"))
        # ★ 时限（2026-10-19）：到点**优雅暂停**（做完当前文件就停，进度保留）✓
        #   可以给 "max_hours": 6，或者 "until": "07:00"（本地时间）
        deadline = _deadline_from(payload)
        if not indexer.start(folder_ids, force, deadline=deadline):
            return {"started": False, "message": "已经有任务在跑"}
        return {"started": True, "deadline": deadline or 0}

    def _deadline_from(payload: dict) -> float | None:
        """从请求里算一个绝对时限（供「计划/全力执行」用）✓

        支持两种写法（都是可选的）：
          "max_hours": 6      → 从现在起 6 小时
          "until": "07:00"    → 到今天的 07:00（已经过了就顺延到明天）
        两个都给时**取更早的那个** ✓（保守）
        """
        import datetime as _dt
        now = _dt.datetime.now()
        out: list[float] = []
        try:
            hours = float(payload.get("max_hours") or 0)
            if hours > 0:
                out.append(time.time() + hours * 3600)
        except (TypeError, ValueError):
            pass
        raw = str(payload.get("until") or "").strip()
        if raw:
            try:
                hh, mm = (raw.split(":") + ["0"])[:2]
                target = now.replace(hour=int(hh), minute=int(mm), second=0,
                                     microsecond=0)
                if target <= now:                    # 已经过了 → 明天这个点
                    target += _dt.timedelta(days=1)
                out.append(target.timestamp())
            except (ValueError, TypeError):
                pass
        return min(out) if out else None

    def _remote_caption_cfg() -> dict | None:
        """（薄包装）真正的逻辑在模块级 caption_model_cfg() 里 ——
        抽出去是为了**脚本能直接测**：后台线程读的到底哪套模型，肉眼看不出来 ✓
        """
        return caption_model_cfg(store)

    def _vl_model_dir() -> str | None:
        """找本地视觉语言（VL）模型目录（本地打标要用它）✓

        不能用配置里的文本模型 —— 它看不到图，只会对每张图瞎编同一段描述
        （app/caption.py 里记着这个坑 ✓）
        """
        try:
            for m in _scan_chat_models():
                arch = str(m.get("arch") or "")
                if "VLForConditional" in arch or "Vision2Seq" in arch:
                    return m.get("path")
        except Exception as exc:                                 # noqa: BLE001
            log.debug("扫 VL 模型失败：%s", exc)
        return None

    @router.get("/api/settings/ocr")
    async def get_ocr_settings(request: Request):
        """读 OCR / 插图打标的设置（设置页显示用）✓

        项目主 2026-10-19："手动选项也保留住（有时可能只需 ocr 一部分内容）"
        → 这些值必须能在界面上改，不用动 config.ini ✓
        """
        _need_login(request)
        from . import ocr as _ocr
        return {
            "enabled": bool(cfg.ocr_enabled),
            "engine": cfg.ocr_engine,
            "max_pages": cfg.ocr_max_pages,          # 0 = 全部做
            "max_images": cfg.ocr_max_images,        # 0 = 全部标
            "page_from": cfg.ocr_page_from,          # 页码范围
            "page_to": cfg.ocr_page_to,              # 0 = 到最后一页
            "engines": _ocr.list_engines(),
            "mineru_api": _ocr.mineru_api_alive(),
            "oxr_api": _ocr.oxr_api_alive(),
            "free_hint": _ocr.free_gpu_hint(),
            "auto_batch": _ocr.auto_batch(),
            "min_chars_per_page": _ocr.MIN_CHARS_PER_PAGE,
            # ★ 2026-10-22 项目主："把连接OCR的功能接口路径填写功能做进界面"
            #   当前生效的路径（界面上显示、可改）
            "paths": {
                "ppocr_python": str(_ocr.ENGINES["ppocr"]["python"]),
                "ppocr_bridge": str(_ocr.ENGINES["ppocr"]["bridge"]),
                "rapid_python": str(_ocr.ENGINES["rapid"]["python"]),
                "rapid_bridge": str(_ocr.ENGINES["rapid"]["bridge"]),
                "mineru_python": str(_ocr.ENGINES["mineru"]["python"]),
                "mineru_bridge": str(_ocr.ENGINES["mineru"]["bridge"]),
                "mineru_dir": (_ocr._ini_ocr().get("mineru_dir")   # noqa: SLF001
                                or os.environ.get("EG2_OCR_MINERU_DIR", "")),
                "oxr_api": (_ocr._ini_ocr().get("oxr_api")      # noqa: SLF001
                            or "http://127.0.0.1:8000"),
                "oxr_bridge": str(_ocr.ENGINES["oxr"]["bridge"]),
            },
        }

    @router.put("/api/settings/ocr")
    async def put_ocr_settings(request: Request, payload: dict = Body(...)):
        """改 OCR 设置（**就地改 config.ini，保留注释**）✓

        可改：enabled（总开关）/ engine（auto|mineru|ppocr|ppocr_cpu|rapid）/
              max_pages（0=全部）/ max_images（每份 PDF 最多标多少插图）
        """
        _need_admin_or_local(request)
        from .config import patch_ini
        from . import ocr as _ocr

        valid_engine = {str(e.get("id")) for e in _ocr.list_engines()} | {"auto"}
        values: dict = {}
        warnings: list[str] = []
        if "enabled" in payload:
            values["ocr_enabled"] = "yes" if payload.get("enabled") else "no"
        if "engine" in payload:
            eng = str(payload.get("engine") or "").strip().lower()
            if eng in valid_engine:
                values["ocr_engine"] = eng
            else:
                warnings.append(f"不认识的引擎 {eng!r}，已忽略")
        if "max_pages" in payload:
            try:
                n = int(payload.get("max_pages"))
                if n < 0:
                    raise ValueError
                values["ocr_max_pages"] = str(n)      # 0 = 全部
            except (TypeError, ValueError):
                warnings.append("页数上限必须是非负整数（0 = 全部）")
        if "max_images" in payload:
            try:
                n = int(payload.get("max_images"))
                if n < 0:
                    raise ValueError
                values["ocr_max_images"] = str(n)      # 0 = 全部
            except (TypeError, ValueError):
                warnings.append("插图上限必须是非负整数（0 = 全部）")
        # ★ 页码范围（2026-10-19 项目主要求："能选页码范围，不要只能从第一页算"）✓
        if "page_from" in payload:
            try:
                n = int(payload.get("page_from"))
                if n < 1:
                    raise ValueError
                values["ocr_page_from"] = str(n)
            except (TypeError, ValueError):
                warnings.append("起始页必须 ≥ 1")
        if "page_to" in payload:
            try:
                n = int(payload.get("page_to"))
                if n < 0:
                    raise ValueError
                values["ocr_page_to"] = str(n)          # 0 = 到最后一页
            except (TypeError, ValueError):
                warnings.append("结束页必须是非负整数（0 = 到最后一页）")
        # ★ 2026-10-22：引擎路径（界面上填，存 config.ini 的 [ocr] 段）
        if isinstance(payload.get("paths"), dict):
            _VALID_KEYS = {"ppocr_python", "ppocr_bridge", "rapid_python",
                           "rapid_bridge", "mineru_python", "mineru_bridge",
                           "mineru_dir", "oxr_api", "oxr_bridge"}
            for k, v in (payload.get("paths") or {}).items():
                k = str(k).strip().lower()
                if k not in _VALID_KEYS:
                    warnings.append(f"不认识的路径项 {k!r}，已忽略")
                    continue
                s = str(v or "").strip()
                is_url = s.startswith("http://") or s.startswith("https://")
                if s and not is_url and not (s.endswith(".exe")
                                             or s.endswith(".py")
                                             or os.path.isdir(s)):
                    # 允许目录（mineru_dir），也允许暂时填一个还没装好的路径
                    warnings.append(f"{k} 看起来不像可执行文件/脚本/目录，仍已保存")
                values[f"ocr_{k}"] = s
        # 交叉校验：起始页不能大于结束页（结束页为 0 表示不限，跳过）
        pf = int(values.get("ocr_page_from", cfg.ocr_page_from) or 1)
        pt = int(values.get("ocr_page_to", cfg.ocr_page_to) or 0)
        if pt > 0 and pf > pt:
            warnings.append(f"起始页 {pf} 大于结束页 {pt}，已按「到最后一页」处理")
            values["ocr_page_to"] = "0"
        if not values:
            return {"ok": False, "message": "没有可改的项", "warnings": warnings}
        ini = Path(cfg.path) if getattr(cfg, "path", None) else ROOT / "config.ini"
        # ★ 路径要写 [ocr] 段，其它设置写 [search] 段（别混在一起）
        _ocr_vals = {k[4:]: v for k, v in values.items() if k.startswith("ocr_")}
        _other = {k: v for k, v in values.items() if not k.startswith("ocr_")}
        changed = []
        if _other:
            changed += patch_ini(ini, "search", _other)
        if _ocr_vals:
            changed += patch_ini(ini, "ocr", _ocr_vals)
        try:
            _ocr.invalidate_cache()        # 让新路径立刻生效 ✓
        except Exception:                                      # noqa: BLE001
            pass
        # 立刻让运行中的 cfg 生效（不用重启服务）✓
        try:
            cfg.reload()
        except Exception as exc:                                 # noqa: BLE001
            log.warning("重载配置失败（改的还是生效的，重启后一定生效）：%s", exc)
        log.info("OCR 设置已改：%s（config.ini 里改了 %s）", values, changed)
        return {"ok": True, "changed": changed, "warnings": warnings,
                "now": {"enabled": cfg.ocr_enabled, "engine": cfg.ocr_engine,
                        "max_pages": cfg.ocr_max_pages,
                        "max_images": cfg.ocr_max_images,
                        "page_from": cfg.ocr_page_from,
                        "page_to": cfg.ocr_page_to}}

    # ---------------- 定时备份（2026-10-20 项目主要求）----------------
    # 项目主原话："把最重要的定时备份数据库功能补上，本地与服务器上都做，
    # 是不是可以双重保证？最好是增量备份，不希望浪费硬盘容量，而且要自定义的
    # 备份数量保证（比如我要求10份，就是保存最新10份，循环替换旧备份，
    # 但要有自定义固定备份功能（不被替换）"
    # → 核心逻辑在 app/dbbackup.py（页级增量），这里只做"看/存/固定/轮换"的接口 ✓
    # ⚠ 这几条都会**真的读写几百 MB**，所以定义成普通 def（FastAPI 自动丢线程池），
    #   不要用 async def —— 否则会卡住整个事件循环（搜索都会变卡）✗
    # ---------------- 快照 / 异地副本（2026-10-20 项目主要求）----------------
    # 「做个快照自动脚本放在界面，能手动快照也能同步进 NAS 或移动硬盘的按钮（要求能自定义路径）」
    # 快照本身还是 run\snapshot.py 干的（它已经把"代码+配置+索引库+还原说明"打成一包），
    # 这里只负责：按钮触发 + 进度 + 目标路径 + 复制过去 ✓
    _SNAP: dict = {"running": False, "syncing": False, "note": "",
                   "last": "", "error": "", "sync_last": ""}

    def _pending_restore() -> dict | None:
        """有没有登记过「待还原」（读文件，很快）✓"""
        try:
            from . import snapshot_restore as _sr
            return _sr.read_pending(ROOT)
        except Exception:                                        # noqa: BLE001
            return None

    @router.get("/api/snapshot/status")
    def snapshot_status(request: Request):
        _need_login(request)
        root = ROOT / "backups"
        items: list[dict] = []
        total = 0.0
        dirs = sorted((p for p in root.glob("snapshot-*") if p.is_dir()),
                      key=lambda p: p.name, reverse=True)
        from . import snapshot_restore as _sr2
        for i, folder in enumerate(dirs):
            # ⚠ 2026-10-20：服务器快照把库放在**根目录**（index.db），本机放在
            #   data/index/index.db → 统一交给 db_path_of() 判断，别只认一种 ✗
            db = _sr2.db_path_of(folder)
            size = 0.0
            # 只对最近 20 份算体积（161 份全扫要好几秒，没必要）
            if i < 20:
                for p in folder.rglob("*"):
                    try:
                        if p.is_file():
                            size += p.stat().st_size
                    except OSError:
                        pass
            note = ""
            man = folder / "MANIFEST.txt"
            if man.exists():
                try:
                    note = str(json.loads(man.read_text(encoding="utf-8",
                                                        errors="replace"))
                               .get("备注") or "")
                except Exception:                                # noqa: BLE001
                    note = ""
            items.append({"name": folder.name, "mb": round(size / 1e6, 1),
                          "has_db": db is not None,
                          "db_mb": (round(db.stat().st_size / 1e6, 1)
                                    if db is not None else 0.0),
                          "db_where": (str(db.relative_to(folder)).replace("\\", "/")
                                       if db is not None else ""),
                          "note": note})
        try:
            # ⚠ 2026-10-20：一共 8.7 万个文件，同步扫要 **19.6 秒** —— 界面会卡在"读取中"✗
            #   → 改成**后台算**：接口立刻返回（used_gb 为 None 时界面显示"统计中"），
            #     算完缓存 5 分钟；文件数变化（新拍快照）时顺手失效 ✓
            now_ts = time.time()
            stale = (now_ts - float(_SNAP.get("used_ts") or 0)) > 60
            if stale and not _SNAP.get("used_busy"):
                _SNAP["used_busy"] = True

                def _calc_used() -> None:
                    tot = 0.0
                    try:
                        for p in root.rglob("*"):
                            try:
                                if not p.is_file():
                                    continue
                                rel = p.relative_to(root)
                            except (OSError, ValueError):
                                continue
                            if rel.parts and rel.parts[0].startswith("snapshot-"):
                                tot += p.stat().st_size
                    except OSError:
                        pass
                    _SNAP["used_gb"] = round(tot / 1e9, 1)
                    _SNAP["used_ts"] = time.time()
                    _SNAP["used_busy"] = False

                threading.Thread(target=_calc_used, name="eg2-snapsize",
                                 daemon=True).start()
            total = _SNAP.get("used_gb")
        except OSError:
            total = None
        target = str(cfg._get("backup", "snapshot_target", "") or "")   # noqa: SLF001
        # ⚠ used_gb 可能是 None（后台还在统计）→ 不能 round()，界面会显示"统计中…" ✓
        # ⚠ 2026-10-20 修：原来只返回 `items[:20]` → 界面上永远只看到 20 条，
        #   项目主问"界面没显示那么多啊"就是这里 ✗
        #   → 全部返回（162 条 × 百来字节，很小），界面自己决定折叠多少条
        return {"ok": True, "count": len(items), "snapshots": items,
                "used_gb": (round(total, 1) if isinstance(total, (int, float)) else None),
                "used_busy": bool(_SNAP.get("used_busy")),
                "pending": _pending_restore(),
                "target": target,
                "target_ok": bool(target) and Path(target).is_dir(),
                "running": _SNAP["running"], "syncing": _SNAP["syncing"],
                "note": _SNAP["note"], "last": _SNAP["last"],
                "sync_last": _SNAP["sync_last"], "error": _SNAP["error"]}

    @router.post("/api/snapshot/run")
    def snapshot_run(request: Request, payload: dict = Body(default={})):
        """立刻拍一份快照（后台跑，约 1~2 分钟）✓"""
        _need_admin_or_local(request)
        if _SNAP["running"]:
            return {"ok": False, "message": "正在拍快照，等它跑完"}
        note = str(payload.get("note") or "").strip() or "界面手动快照"
        _SNAP.update(running=True, note="正在拍快照（约 1~2 分钟）…", error="", last="")

        def _work() -> None:
            try:
                r = subprocess.run(
                    [sys.executable, "-u", "run/snapshot.py", note],
                    cwd=str(ROOT), capture_output=True, timeout=3600)
                out = (r.stdout or b"").decode("utf-8", "replace")
                ok = r.returncode == 0 and "完成：" in out
                done = ""
                for line in out.splitlines():
                    if line.startswith("完成："):
                        done = line.replace("完成：", "").strip()
                _SNAP.update(running=False, last=time.strftime("%H:%M:%S"),
                             note=(f"已生成 {Path(done).name}" if done else "已结束"),
                             error="" if ok else out[-400:])
                log.info("界面快照完成：%s", done or "(未解析到路径)")
            except Exception as exc:                             # noqa: BLE001
                _SNAP.update(running=False, error=f"{type(exc).__name__}: {exc}")
                log.warning("界面快照失败：%s", exc)

        threading.Thread(target=_work, name="eg2-snapshot", daemon=True).start()
        return {"ok": True, "message": "已开始拍快照"}

    @router.post("/api/snapshot/sync")
    def snapshot_sync(request: Request):
        """把「最新一份快照 + 增量备份」复制到自定义目录（NAS / 移动硬盘）✓"""
        _need_admin_or_local(request)
        if _SNAP["syncing"]:
            return {"ok": False, "message": "正在同步，等它跑完"}
        target = str(cfg._get("backup", "snapshot_target", "") or "")   # noqa: SLF001
        if not target:
            raise HTTPException(status_code=400,
                                detail="先填一个目标目录（NAS 或移动硬盘上的路径）")
        tgt_root = Path(target)
        if not tgt_root.is_dir():
            raise HTTPException(status_code=400,
                                detail=f"目标目录不存在或没挂载：{target}")
        _SNAP.update(syncing=True, note="正在同步到 " + target)
        snaps = sorted(p for p in (ROOT / "backups").glob("snapshot-*") if p.is_dir())

        def _work() -> None:
            import shutil
            freed: list[str] = []
            try:
                dest = tgt_root / "lingxi-备份"
                dest.mkdir(parents=True, exist_ok=True)
                if snaps:
                    latest = snaps[-1]
                    out = dest / "snapshots" / latest.name
                    if out.exists():
                        freed.append(f"（{latest.name} 已在目标里，跳过）")
                    else:
                        shutil.copytree(latest, out)
                        freed.append(f"快照 {latest.name} 已复制")
                dbdir = ROOT / "backups" / "db"
                if dbdir.exists():
                    shutil.copytree(dbdir, dest / "db", dirs_exist_ok=True)
                    freed.append("增量备份已同步")
                _SNAP.update(syncing=False, sync_last=time.strftime("%H:%M:%S"),
                             note="同步完成：" + "；".join(freed))
                log.info("快照同步完成：%s → %s", "；".join(freed), dest)
            except Exception as exc:                             # noqa: BLE001
                _SNAP.update(syncing=False,
                             error=f"{type(exc).__name__}: {exc}",
                             note="同步失败")
                log.warning("快照同步失败：%s", exc)

        threading.Thread(target=_work, name="eg2-snapsync", daemon=True).start()
        return {"ok": True, "message": "已开始同步"}

    @router.put("/api/snapshot/target")
    def snapshot_target(request: Request, payload: dict = Body(...)):
        """自定义同步目标目录（NAS 挂载点 / 移动硬盘盘符）✓"""
        _need_admin_or_local(request)
        path = str(payload.get("path") or "").strip()
        if path:
            p = Path(path)
            if not p.is_dir():
                raise HTTPException(status_code=400,
                                    detail=f"这个目录不存在或还没挂载：{path}")
        from .config import patch_ini
        ini = Path(cfg.path) if getattr(cfg, "path", None) else ROOT / "config.ini"
        patch_ini(ini, "backup", {"snapshot_target": path})
        try:
            cfg.reload()
        except Exception as exc:                                 # noqa: BLE001
            log.warning("重载配置失败：%s", exc)
        log.info("快照同步目标改成：%s", path or "（空）")
        return {"ok": True, "target": path}

    @router.post("/api/snapshot/restore")
    def snapshot_restore(request: Request, payload: dict = Body(...)):
        """登记「还原到某一份快照」——真正的替换在**下次启动服务之前**做 ✓

        项目主 2026-10-20："在按钮旁也应有一个还原按钮吧？要不如何操作还原呢？"
        + "这个功能，服务器版也希望有" + "服务器版应该只有管理原有权限操作"
        → 写 pending，由 run\\restart_server.py（本机）/ systemd ExecStartPre（服务器）
          在服务停着的窗口里执行；换库前会先把现有库改名留底 ✓
        ⚠ 只有管理员能碰（还原会把整个站的数据退回去）
        """
        _need_admin_or_local(request)
        from . import snapshot_restore as _sr
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="要指定是哪一份快照")
        code = bool(payload.get("code"))
        db = bool(payload.get("db"))
        if not code and not db:
            raise HTTPException(status_code=400, detail="至少要选一样（代码 / 索引库）")
        try:
            data = _sr.write_pending(ROOT, name, code=code, db=db,
                                     by=str((_who(request) or {}).get("name") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        log.info("登记还原：%s（代码=%s 库=%s）", name, code, db)
        return {"ok": True, "pending": data,
                "message": ("已登记。重启服务时自动执行"
                            "（本机：关掉界面再开 / 跑 restart_server；"
                            "服务器：sudo systemctl restart lingxi）")}

    @router.get("/api/snapshot/restore-pending")
    def snapshot_restore_pending(request: Request):
        _need_login(request)
        from . import snapshot_restore as _sr
        data = _sr.read_pending(ROOT)
        return {"ok": True, "pending": data}

    @router.post("/api/snapshot/restore-cancel")
    def snapshot_restore_cancel(request: Request):
        _need_admin_or_local(request)
        from . import snapshot_restore as _sr
        gone = _sr.cancel_pending(ROOT)
        log.info("取消已登记的还原：%s", gone)
        return {"ok": True, "cancelled": gone}

    @router.get("/api/backup/status")
    def backup_status(request: Request):
        # ★ 2026-10-20 项目主："服务器上的打标/备份可以挂载主库上，但默认只有管理员可用"
        #   状态也一并收紧（普通用户在成员版界面里根本看不到这张卡）✓
        _need_admin_or_local(request)
        from . import dbbackup as _bk
        st = _bk.status(cfg)
        st["should_run"], st["why"] = _bk.should_run(cfg)
        return st

    # ---------- 重启服务（2026-10-27 项目主：服务器版要能手动重启）
    @router.post("/api/admin/restart")
    def admin_restart(request: Request, payload: dict = Body(default={})):
        """重启灵犀服务（仅管理员；服务器版）。

        为什么要它：NAS 重启滞后时，程序对网络盘的句柄可能已经失效，
        "重新检查目录"仍是假的 —— 重启进程是最干脆的恢复手段。
        做法：**先回响应，再退出进程**；systemd 的 `Restart=on-failure`
        会把它拉起来（退出码 1 视作失败，故意这样，保证一定会重启 ✓）。
        本机版请用 `restart.bat`（那里要处理窗口和浏览器，不能走这条路 ✗）。
        """
        if _can_index_here():
            raise HTTPException(
                status_code=400,
                detail="本机版请用 restart.bat 重启（会一并处理窗口和浏览器）")
        # ★ 项目主 2026-10-27："服务器版只需管理员有权限重启服务"
        #   所以这里**只认 role=admin**，不套用"本机登录即可"那套 ✓
        u = _who(request) if _auth_on() else None
        if _auth_on() and not u:
            raise HTTPException(status_code=401, detail="请先登录")
        if not u or str(u.get("role") or "") != "admin":
            raise HTTPException(status_code=403,
                                detail="只有管理员能重启服务器服务")
        if str(payload.get("confirm") or "") != "restart":
            raise HTTPException(status_code=400, detail="要确认：confirm=restart")

        delay = 0.6
        log.info("管理员请求重启服务，%.1f 秒后退出（systemd 会拉起来）…", delay)

        def _do() -> None:
            time.sleep(delay)
            os._exit(1)                       # 非 0 → Restart=on-failure 一定重启 ✓

        threading.Thread(target=_do, daemon=True).start()
        return {"ok": True, "restarting": True,
                "note": "正在重启，约 25~40 秒后可用（模型要重新加载）"}

    @router.post("/api/backup/run")
    def backup_run(request: Request, payload: dict = Body(default={})):
        """立刻存一份（没变化就不落盘）✓"""
        _need_admin_or_local(request)
        from . import dbbackup as _bk
        res = _bk.save(cfg, note=str(payload.get("note") or ""))
        log.info("手动备份：%s", res.get("message"))
        return res

    @router.post("/api/backup/pin")
    def backup_pin(request: Request, payload: dict = Body(...)):
        """📌 固定 / 取消固定某一份（固定的永不被轮换删掉）✓"""
        _need_admin_or_local(request)
        stamp = str(payload.get("stamp") or "").strip()
        if not stamp:
            raise HTTPException(status_code=400, detail="缺少 stamp（哪一份）")
        from . import dbbackup as _bk
        data = _bk.pin(cfg, stamp, bool(payload.get("pinned", True)))
        log.info("备份 %s 固定标记改成：%s", stamp, data.get("pinned"))
        return {"ok": True, "stamp": data["stamp"], "pinned": data["pinned"],
                "status": _bk.status(cfg)}

    @router.post("/api/backup/prune")
    def backup_prune(request: Request, payload: dict = Body(default={})):
        """立刻按「保留份数」轮换（固定份不参与）✓"""
        _need_admin_or_local(request)
        from . import dbbackup as _bk
        keep = payload.get("keep")
        res = _bk.prune(cfg, int(keep) if keep not in (None, "") else None)
        res["status"] = _bk.status(cfg)
        return res

    @router.post("/api/backup/verify")
    def backup_verify(request: Request, payload: dict = Body(...)):
        """重建某一份并比对摘要（确认这份**真能恢复**）✓"""
        _need_admin_or_local(request)
        stamp = str(payload.get("stamp") or "").strip()
        if not stamp:
            raise HTTPException(status_code=400, detail="缺少 stamp（哪一份）")
        from . import dbbackup as _bk
        return _bk.verify(cfg, stamp)

    @router.put("/api/backup/settings")
    def backup_settings(request: Request, payload: dict = Body(...)):
        """改总开关 / 间隔小时 / 保留份数（就地改 config.ini，保留注释）✓"""
        _need_admin_or_local(request)
        from .config import patch_ini
        values: dict = {}
        warnings: list[str] = []
        if "enabled" in payload:
            values["enabled"] = "yes" if payload.get("enabled") else "no"
        if "interval_hours" in payload:
            try:
                h = float(payload.get("interval_hours"))
                if not 1 <= h <= 24 * 30:
                    raise ValueError
                values["interval_hours"] = str(int(h) if h == int(h) else h)
            except (TypeError, ValueError):
                warnings.append("间隔小时要填 1~720 之间的数")
        if "keep" in payload:
            try:
                n = int(payload.get("keep"))
                if not 1 <= n <= 200:
                    raise ValueError
                values["keep"] = str(n)
            except (TypeError, ValueError):
                warnings.append("保留份数要填 1~200 之间的整数")
        if not values:
            return {"ok": False, "message": "没有可改的项", "warnings": warnings}
        ini = Path(cfg.path) if getattr(cfg, "path", None) else ROOT / "config.ini"
        changed = patch_ini(ini, "backup", values)
        try:
            cfg.reload()
        except Exception as exc:                                 # noqa: BLE001
            log.warning("重载配置失败（改的还是生效的，重启后一定生效）：%s", exc)
        from . import dbbackup as _bk
        log.info("备份设置已改：%s（config.ini 里改了 %s）", values, changed)
        return {"ok": True, "changed": changed, "warnings": warnings,
                "status": _bk.status(cfg)}

    # ---------------- 本机 / 服务器 路径映射（2026-10-20，项目主要求）----------------
    @router.get("/api/folders/{folder_id}/paths")
    async def get_folder_paths(folder_id: int, request: Request):
        """看这个文件夹的三个路径 + 自动建议 ✓

        项目主："我桌面版使用了 \\\\NAS\\photo\\家庭相册 这种网络文件夹，
        它就应该在旁有个服务器对应的路径可以自定义"
        + "本机的文件夹也需可以自定义设置同步目录"
        """
        _need_login(request)
        f = store.get_folder(int(folder_id))
        if not f:
            raise HTTPException(status_code=404, detail="没有这个文件夹")
        from . import serverpath as _sp
        p = store.folder_paths(int(folder_id))
        return {"ok": True, "id": int(folder_id),
                "path": p["path"],            # 权威键（数据库里索引的依据）
                "local": p["local"],          # 本机实际目录（可自定义）
                "server": p["server"],        # 服务器实际目录（可自定义）
                "suggest_server": _sp.suggest_server_path(p["path"]),
                "suggest_help": _sp.display_help(p["path"])}

    @router.put("/api/folders/{folder_id}/paths")
    async def set_folder_paths(folder_id: int, request: Request,
                               payload: dict = Body(default={})):
        """设"本机实际目录"和"服务器实际目录"（都可留空 = 跟随默认）✓

        payload: {"local": "D:\\data\\家庭相册", "server": "/mnt/nas_photo/家庭相册"}
        留空字符串 = 清除该项 ✓
        """
        _need_builder(request)
        f = store.get_folder(int(folder_id))
        if not f:
            raise HTTPException(status_code=404, detail="没有这个文件夹")
        msgs = []
        if "local" in payload:
            v = str(payload.get("local") or "").strip()
            store.set_folder_local_alias(int(folder_id), v)
            msgs.append(f"本机目录 = {v or '（就用原路径）'}")
        if "server" in payload:
            v = str(payload.get("server") or "").strip()
            store.set_folder_server_path(int(folder_id), v)
            msgs.append(f"服务器目录 = {v or '（不设，自动建议）'}")
        p = store.folder_paths(int(folder_id))
        log.info("文件夹 #%s 路径映射已更新：%s", folder_id, "；".join(msgs))
        return {"ok": True, "id": int(folder_id), "paths": p,
                "message": "已保存 ✓ " + "；".join(msgs)}

    # ---------------- 单份 PDF 的 OCR 页码（优先级最高，2026-10-19）----------------
    @router.get("/api/files/{file_id}/ocr-pages")
    async def get_file_ocr_pages(file_id: int, request: Request):
        """看某份 PDF 单独设的页码范围 + 它的 OCR 进度 ✓"""
        _need_login(request)
        r = store.get_file(int(file_id))
        if not r:
            raise HTTPException(status_code=404, detail="没有这个文件")
        pf, pt = store.ocr_pages_of(file_id)
        done, limit = store.ocr_progress(file_id)
        return {"ok": True, "file_id": int(file_id), "name": r["name"],
                "path": r["path"], "kind": r["kind"],
                "page_from": pf, "page_to": pt,      # (0,0) = 跟随全局
                "ocr_pages": done, "ocr_limit": limit,
                "global_from": cfg.ocr_page_from, "global_to": cfg.ocr_page_to,
                "source": "自设" if (pf > 0 or pt > 0) else "跟随全局"}

    @router.put("/api/files/{file_id}/ocr-pages")
    async def set_file_ocr_pages(file_id: int, request: Request,
                                 payload: dict = Body(default={})):
        """给**某一份** PDF 单独设页码范围 —— **优先级最高**（个别 > 全局）✓

        项目主 2026-10-19："应该是要某个特定 PDF 单独设页码范围，这个设置应该是
        最高确认，比如全局自动全部，个别取页码范围，以个别设置为准"

        payload: {"page_from": 50, "page_to": 80}
                 {"page_from": 0,  "page_to": 0}   → 清除，恢复跟随全局 ✓
        """
        _need_admin_or_local(request)
        r = store.get_file(int(file_id))
        if not r:
            raise HTTPException(status_code=404, detail="没有这个文件")
        try:
            pf = int(payload.get("page_from") or 0)
            pt = int(payload.get("page_to") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="页码必须是整数") from None
        if pf < 0 or pt < 0:
            raise HTTPException(status_code=400, detail="页码不能是负数")
        msg = ""
        if pf > 0 and pt > 0 and pf > pt:
            pf, pt = pt, pf
            msg = f"起止写反了，已按 {pf}~{pt} 处理；"
        if pf == 0 and pt > 0:
            pf = 1                       # 只给结束页 → 从头做到那儿 ✓
        store.set_ocr_pages(file_id, pf, pt)
        log.info("单份 PDF 页码已设：#%s %s → %s~%s（进度已清零，下次重扫生效）",
                 file_id, r["name"], pf or "跟随全局", pt or "跟随全局")
        scope = "已清除，恢复跟随全局" if (pf == 0 and pt == 0) else f"已设为第 {pf}~{pt or '末'} 页"
        return {"ok": True, "file_id": int(file_id), "page_from": pf, "page_to": pt,
                "message": f"{scope} ✓（这个设置**优先于全局**；下次扫这份文件时生效，"
                           f"进度已清零会重新按新范围做）" + msg}

    @router.get("/api/gpu/status")
    async def gpu_status_api(request: Request):
        """显卡现在被谁占着、有几个在排队 + 各 OCR 引擎可用性（界面显示用）✓

        项目主 2026-10-19："OCR 与打标同时进行，不是要撞显存吗？给个顺序" ✓
        → 这个接口让人**看得见**谁在用显卡、谁在排队 ✓
        """
        _need_login(request)
        from . import ocr as _ocr
        st = dict(_ocr.gpu_status())
        st["free_hint"] = _ocr.free_gpu_hint()
        st["mineru_api"] = _ocr.mineru_api_alive()
        st["engines"] = _ocr.list_engines()
        st["night"] = dict(_NIGHT)
        return st

    @router.post("/api/index/plan")
    async def index_plan(request: Request, payload: dict = Body(default={})):
        """📋 **计划增量更新** —— 只算不做，把"接下来要干的事"列出来 ✓

        项目主 2026-10-19 的用法：白天放完文件点一下，看清单心里有数；
        夜里再点「全力执行」让它按计划跑 ✓

        这里**不扫描目录、不调模型、不占显存**（秒回）：
          · 有哪些文件夹、上次什么时候扫的
          · 队列里有多少张插图待打标（按文件夹分组）
          · OCR 开关/引擎/页数上限当前是什么
        """
        u = _need_login(request)
        # 本机（建库机）= 自己的机器，和服务器管理员同权 ✓
        is_admin = _can_index_here() or (not u) or u.get("role") == "admin"
        folder_ids = payload.get("folder_ids") or None
        if not is_admin:
            from .multiuser import writable_folder_ids as _writable
            mine = {int(x) for x in (_writable(cfg.db_path, u) or [])}
            if folder_ids:
                folder_ids = [int(x) for x in folder_ids if int(x) in mine]
            else:
                folder_ids = sorted(mine)

        folders = store.list_folders(stats=False)
        if folder_ids:
            folders = [f for f in folders if int(f["id"]) in {int(x) for x in folder_ids}]

        qstats = store.caption_queue_stats()
        pending = store.caption_queue_list(state="pending", folder_ids=folder_ids,
                                           limit=2000)
        per_folder: dict[int, int] = {}
        for it in pending:
            per_folder[int(it["folder_id"])] = per_folder.get(int(it["folder_id"]), 0) + 1

        items = []
        for f in folders:
            fid = int(f["id"])
            items.append({
                "id": fid, "path": str(f["path"]),
                "last_scan": f.get("last_scan") or 0,
                "caption_mode": ((f.get("config") or {}).get("caption_mode") or "off"),
                "caption_model": ((f.get("config") or {}).get("caption_model") or ""),
                "caption_pending": per_folder.get(fid, 0),
            })
        total_imgs = sum(per_folder.values())
        est_caption_sec = total_imgs * 6          # 每张按 6 秒粗估（外部模型）
        plan = {
            "ok": True,
            "folders": items,
            "totals": {
                "folders": len(items),
                "caption_pending": total_imgs,
                "est_caption_minutes": round(est_caption_sec / 60, 1),
            },
            "ocr": {"enabled": bool(cfg.ocr_enabled), "engine": cfg.ocr_engine,
                    "max_pages": cfg.ocr_max_pages,
                    "max_images": cfg.ocr_max_images},
            "queue": qstats,
            "hint": ("点「🚀 全力执行」会按这个计划跑：先腾显存 → 索引 → "
                     "插图打标 → 收尾（停 MinerU、把嵌入模型挪回显卡）。"
                     "可以设时限（到点优雅暂停，进度保留）✓"),
        }
        global _LAST_PLAN
        _LAST_PLAN = plan
        return plan

    @router.post("/api/index/run-plan")
    async def index_run_plan(request: Request, payload: dict = Body(default={})):
        """🚀 **全力执行** —— 按计划全力跑（夜里关屏幕前点一下）✓

        编排顺序（项目主要求"给个顺序，别撞显存"✓）：
          ① 腾显存：把嵌入模型挪到 CPU
          ② 索引（受时限约束）
          ③ 插图打标（走 gpu_slot，和 OCR 排队；调 API 的不排队）
          ④ 收尾：停 MinerU（把它的显存也还回来）→ 把嵌入模型挪回显卡

        参数（都可选）：
          folder_ids  只跑这几个
          force       重建（默认增量）
          max_hours / until   时限
          free_gpu    true = 跑前腾显存（默认 true）
          caption     false = 这轮不打标（只索引）
          stop_mineru 跑完是否停 MinerU（默认 true）
        """
        _need_builder(request)
        u = _need_login(request)
        if engine.model is None:
            raise HTTPException(status_code=503, detail="模型还没加载完，请稍候")
        if indexer.is_running():
            return {"started": False, "message": "已经有任务在跑"}
        if _NIGHT["running"]:
            return {"started": False, "message": "「全力执行」已经在跑了"}

        folder_ids = [int(x) for x in (payload.get("folder_ids") or [])] or None
        force = bool(payload.get("force"))
        free_gpu = bool(payload.get("free_gpu", True))
        do_caption = bool(payload.get("caption", True))
        stop_mineru = bool(payload.get("stop_mineru", True))
        deadline = _deadline_from(payload)

        _NIGHT["running"] = True
        _NIGHT["note"] = "准备中…"

        def _worker() -> None:
            import datetime as _dt
            from . import caption_run as _cr
            from . import ocr as _ocr
            log.info("「全力执行」开始（时限 %s）",
                     (_dt.datetime.fromtimestamp(deadline).strftime("%H:%M")
                      if deadline else "不限"))
            try:
                # ① 腾显存
                if free_gpu:
                    _NIGHT["note"] = "腾显存：把嵌入模型挪到 CPU"
                    try:
                        _embedding_to("cpu")
                        log.info("已腾显存：%s", _ocr.free_gpu_hint())
                    except Exception as exc:                     # noqa: BLE001
                        log.warning("腾显存失败（继续）：%s", exc)
                # ② 索引（受时限）
                _NIGHT["note"] = "索引中…"
                indexer.start(folder_ids, force, deadline=deadline)
                while indexer.is_running():
                    time.sleep(5)
                # ③ 插图打标（受时限 + 过 gpu_slot）
                if do_caption and (not deadline or time.time() < deadline):
                    _NIGHT["note"] = "插图打标中…"
                    remote = _remote_caption_cfg()
                    vldir = None if remote else _vl_model_dir()
                    if not remote and not vldir:
                        log.warning("既没有外部打标模型、也没找到本地 VL 模型 → "
                                    "跳过插图打标")
                    else:
                        # ⚠ 2026-10-20 修：run_queue 一次只取 200 条（队列表的默认上限）✗
                        #   实测 #88 排了 777 张 → 跑完一批就"结束"了，
                        #   界面上看着跑完了，其实还剩 577 张 ✗
                        #   → 这里**分批接着跑，直到队列空 / 到时限** ✓
                        batch = 200
                        total_done = total_failed = 0
                        round_no = 0
                        while not deadline or time.time() < deadline:
                            round_no += 1
                            _NIGHT["note"] = (f"插图打标中…（第 {round_no} 批，"
                                              f"累计完成 {total_done} 张）")
                            try:
                                res = _cr.run_queue(cfg, store, engine, _chat_engine(),
                                                    folder_ids=folder_ids, limit=batch,
                                                    remote=remote, vl_dir=vldir,
                                                    deadline=deadline,
                                                    source="night")
                            except Exception as exc:             # noqa: BLE001
                                # 一批出错不该把整夜任务打断（原来这里就有这层保护）
                                log.warning("插图打标这一批失败（已做的不回退）：%s", exc)
                                break
                            done_n = int(res.get("done") or 0)
                            total_done += done_n
                            total_failed += int(res.get("failed") or 0)
                            log.info("插图打标第 %d 批：%s", round_no,
                                     {k: res.get(k) for k in
                                      ("done", "failed", "skipped", "picked")})
                            pending = int((res.get("queue") or {}).get("pending") or 0)
                            _NIGHT["note"] = (f"插图打标中…（累计完成 {total_done} 张，"
                                              f"还剩 {pending} 张）")
                            if int(res.get("picked") or 0) == 0 or pending <= 0:
                                break        # 队列空了，收工 ✓
                            if done_n == 0 and int(res.get("failed") or 0) == 0:
                                break        # 这批一张都没处理（不是时限问题）→ 别死循环
                        log.info("插图打标结束：累计成功 %d / 失败 %d（共 %d 批）",
                                 total_done, total_failed, round_no)
            except Exception as exc:  # noqa: BLE001
                log.exception("「全力执行」出错")
                _NIGHT["note"] = f"出错：{exc}"
            finally:
                # ④ 收尾
                if stop_mineru:
                    _NIGHT["note"] = "收尾：停 MinerU 回收显存"
                    try:
                        ok, msg = _ocr.stop_mineru_api()
                        log.info("停 MinerU：%s %s", ok, msg)
                    except Exception as exc:                     # noqa: BLE001
                        log.warning("停 MinerU 失败：%s", exc)
                if free_gpu:
                    _NIGHT["note"] = "收尾：把嵌入模型挪回显卡"
                    try:
                        _embedding_to("cuda")
                    except Exception as exc:                     # noqa: BLE001
                        log.warning("挪回显卡失败：%s", exc)
                _NIGHT["running"] = False
                _NIGHT["note"] = "已结束"
                log.info("「全力执行」结束；%s", _ocr.free_gpu_hint())

        threading.Thread(target=_worker, name="eg2-night", daemon=True).start()
        return {"started": True, "deadline": deadline or 0,
                "plan": _LAST_PLAN}


    @router.post("/api/index/cancel")
    async def index_cancel(request: Request):
        # 取消没有破坏性（只是停下），登录用户就能取消自己的任务 ✓
        _need_login(request)
        indexer.cancel()
        return {"ok": True}

    @router.post("/api/open-file")
    async def open_file(request: Request, payload: dict = Body(...)):
        """用系统默认程序打开文件。

        PDF/Excel 提取出来的只有文字，看不到原表格版式，
        所以预览里给个按钮直接调系统程序打开。
        """
        import os as _os
        import subprocess as _sp
        import sys as _sys

        from pathlib import Path as _P
        raw = str(payload.get("path") or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="要给路径")
        _need_path_read(request, raw)                          # 别人的私有文件别想打开 ✓
        p = _P(raw)
        # ★ 服务器上库里存的是 Windows 路径（\\192.168.1.20\...），Linux 打不开 ✗
        #   项目主 2026-10-20："服务器接上" → 换成服务器视角的路径再试 ✓
        if not p.exists():
            try:
                from . import serverpath as _sp
                if _sp.is_server():
                    alt = _sp.suggest_server_path(raw)
                    if alt and _P(alt).exists():
                        p = _P(alt)
                        raw = alt
                        log.info("服务器上把 Windows 路径换成：%s", alt)
            except Exception:                                 # noqa: BLE001
                pass
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"文件不存在：{raw}")
        # 只允许打开已授权根目录下的文件
        try:
            real = safe_resolve(p)
            roots = [safe_resolve(r) for r in _allowed_roots()]
            if not any(str(real).lower().startswith(str(r).lower()) for r in roots):
                raise HTTPException(status_code=403, detail="该路径不在已授权的文件夹里")
        except HTTPException:
            raise
        except Exception:                                     # noqa: BLE001
            pass
        try:
            if _sys.platform.startswith("win"):
                _os.startfile(str(p))                         # noqa: S606
            elif _sys.platform == "darwin":
                _sp.Popen(["open", str(p)])                  # noqa: S603,S607
            else:
                _sp.Popen(["xdg-open", str(p)])              # noqa: S603,S607
        except Exception as exc:                              # noqa: BLE001
            raise HTTPException(status_code=500,
                                detail=f"打开失败：{exc}") from exc
        return {"ok": True, "path": str(p)}

    # ======================================================== 显存控制 / 外部模型
    @router.get("/api/gpu")
    async def gpu_state():
        """看当前显存占用，以及嵌入模型在哪。"""
        import torch as _th
        used = free = total = 0
        if _th.cuda.is_available():
            free, total = _th.cuda.mem_get_info()
            used = total - free
        return {
            "total_mb": round(total / 1048576),
            "used_mb": round(used / 1048576),
            "free_mb": round(free / 1048576),
            "emb_device": str(getattr(engine, "device", "?")),
            "loaded": engine.model is not None,
        }

    @router.post("/api/gpu/embedding")
    async def gpu_embedding(request: Request, payload: dict = Body(default={})):
        """把嵌入模型挪到 CPU 或显卡。

        用途：你要用满显卡跑别的程序（比如 llama.cpp 的 27B）时，
        点一下把嵌入模型挪到 CPU，腾出约 2.3 GB。
        搜索仍然可用，只是编码查询慢 1-2 秒。

        多人：切设备影响所有人的搜索速度 → 只有管理员能切 ✓
        """
        _need_admin_or_local(request)
        where = str(payload.get("device") or "cpu").lower()
        if where not in ("cpu", "cuda"):
            raise HTTPException(status_code=400, detail="device 只能是 cpu 或 cuda")
        if engine.model is None:
            return {"ok": True, "device": where, "note": "模型还没加载，无需操作"}
        try:
            _embedding_to(where)
        except Exception as exc:                              # noqa: BLE001
            raise HTTPException(status_code=500,
                                detail=f"切换失败：{exc}") from exc
        import torch as _th
        free = _th.cuda.mem_get_info()[0] / 1048576 if _th.cuda.is_available() else 0
        return {"ok": True, "device": where, "free_mb": round(free),
                "note": f"嵌入模型已移到 {where.upper()}，显存空闲 {round(free)} MB"}

    @router.post("/api/remote/probe")
    async def remote_probe(request: Request, payload: dict = Body(...)):
        """探测一个外部模型服务（Ollama / OpenAI 兼容）上有什么模型。

        多人：这是"服务器主动去访问一个你填的地址"（SSRF 面）+ 属于全局设置
        → 只有管理员能用 ✓
        """
        _need_admin_or_local(request)
        from .vlm_client import probe
        url = str(payload.get("base_url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="要填地址")
        return probe(url, api_key=str(payload.get("api_key") or ""),
                     timeout=int(payload.get("timeout") or 90),
                     extra_headers=payload.get("headers") or None)

    @router.post("/api/remote/test")
    async def remote_test(request: Request, payload: dict = Body(...)):
        """用外部模型给一张现成的图写描述，验证它到底能不能看图。

        多人：同 remote/probe，属全局设置 + 会读一张图 → 只有管理员 ✓
        """
        _need_admin_or_local(request)
        from pathlib import Path as _P

        from .model import decode_image as _dec
        from .vlm_client import caption_one
        url = str(payload.get("base_url") or "").strip()
        model = str(payload.get("model") or "").strip()
        fid = int(payload.get("file_id") or 0)
        if not url or not model:
            raise HTTPException(status_code=400, detail="要填地址和模型名")
        if not fid:
            row = store._query(
                "SELECT id FROM files WHERE kind='image' AND folder_id IN "
                "(SELECT id FROM folders) LIMIT 1")
            fid = int(row[0]["id"]) if row else 0
        if not fid:
            raise HTTPException(status_code=400, detail="库里没有可用图片")
        r = store._query("SELECT path, name FROM files WHERE id=?", (fid,))[0]
        side = max(224, min(int(payload.get("max_side") or 768), 1600))
        mtok = max(64, min(int(payload.get("max_tokens") or 800), 2048))
        im = _dec(_P(r["path"]), max_side=side).convert("RGB")
        import time as _t
        t0 = _t.perf_counter()
        try:
            cap = caption_one(url, model, im, timeout=1800,
                              max_side=side, max_tokens=mtok,
                              api_key=str(payload.get("api_key") or ""),
                              extra_headers=payload.get("headers") or None)
        except Exception as exc:                              # noqa: BLE001
            raise HTTPException(status_code=502,
                                detail=f"调用失败：{exc}") from exc
        return {"ok": True, "file": r["name"], "caption": cap,
                "max_side": side, "max_tokens": mtok,
                "elapsed": round(_t.perf_counter() - t0, 1)}

    # ======================================================== 图片打标（VL）
    # ★ 2026-10-20 项目主要求：
    #   "现在只有针对文件夹打标按钮，那就在旁边增加一个 PDF 插图的打标按钮吧，
    #    我手动也方便，也要可以随时暂停开始功能及并到计划中，让晚上全力执行功能"
    #   + "现在项目在图片打标，我在项目界面也看不到进程啊，这个我怎么监控？"
    #   → 这一组接口给「PDF 插图打标（队列）」用：
    #       status  看进度（第几条/总共多少/当前文件/速度/预计剩余）
    #       start   后台开始（不挂死 HTTP）
    #       pause / resume / stop  随时暂停、继续、停下（剩下留在队列里）
    #   注意和上面 /api/vl/* 的区别：
    #     /api/vl/*       = 给**图片文件**打描述（app/caption.py，会挪嵌入模型腾显存）
    #     /api/caption/queue/* = 给 **PDF 里的插图**打描述（caption_queue，外部 API 可并行）
    @router.get("/api/caption/queue/status")
    async def caption_queue_status(request: Request):
        _need_login(request)
        from . import caption_run as _cr
        st = _cr.run_state()
        st["queue"] = store.caption_queue_stats()
        st["folder_ids"] = sorted({int(m.get("folder_id") or 0)
                                   for m in store.caption_queue_list(state="pending",
                                                                     limit=2000)})
        return st

    @router.get("/api/caption/queue/files")
    async def caption_queue_files(request: Request, folder_ids: str = ""):
        """队列里"还有待办"的文件清单（界面选文件用）✓

        项目主 2026-10-20："pdf打标按钮需要指定文件，不能每次只能全部"
        """
        _need_login(request)
        fids = [int(x) for x in folder_ids.split(",") if x.strip().isdigit()] or None
        items = store.caption_queue_files(fids)
        priv = private_folder_ids(store)
        for it in items:
            it["privacy"] = int(it.get("folder_id") or 0) in priv
            # 有效模型：文件自己的 > 文件夹的 > 默认
            own = str(it.get("file_caption_model") or "").strip().lower()
            if int(it.get("folder_id") or 0) in priv:
                it["effective"] = "local"
            elif own in ("local", "remote"):
                it["effective"] = own
            else:
                it["effective"] = "folder"
        return {"ok": True, "files": items,
                "privacy_folders": sorted(priv)}

    @router.post("/api/caption/queue/file-model")
    async def caption_queue_file_model(request: Request, payload: dict = Body(...)):
        """设置**某一份文件**的打标模型（'' 跟随文件夹 / local / remote）✓

        项目主："针对我某些文件隐私效果需要用到本地"
        """
        _need_admin_or_local(request)
        ids = [int(x) for x in (payload.get("file_ids") or [])]
        if not ids and payload.get("file_id"):
            ids = [int(payload["file_id"])]
        if not ids:
            raise HTTPException(status_code=400, detail="要指定 file_ids")
        model = str(payload.get("model") or "").strip().lower()
        if model not in ("", "local", "remote"):
            raise HTTPException(status_code=400, detail="model 只能是 空/local/remote")
        n = 0
        for fid in ids:
            if store.set_file_caption_model(fid, model):
                n += 1
        log.info("把 %d 份文件的打标模型设为 %r", n, model or "跟随文件夹")
        return {"ok": True, "updated": n, "model": model}

    @router.put("/api/folders/{folder_id}/privacy")
    async def set_folder_privacy(request: Request, folder_id: int,
                                 payload: dict = Body(default={})):
        """🔒 隐私文件夹开关（内容**永不出这台机器**）✓

        项目主 2026-10-20："我的隐私文件统统放在里面，这样设置更方便"
                          + "这样就是用外部模型也搜索不到"
        打开后：① 打标强制走本地视觉模型（图片不出机器）
                ② 问答用外部模型时，它的片段一条都不发出去
        ⚠ 和 `visibility=private`（普通用户看不到）不是一回事 ✓
        """
        _need_builder(request)
        folder = store.get_folder(int(folder_id))
        if folder is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        on = bool(payload.get("privacy"))
        if not store.set_folder_privacy(int(folder_id), on):
            raise HTTPException(status_code=404, detail="文件夹不存在")
        log.info("文件夹 #%s 的「隐私」标记改成 %s", folder_id, "开" if on else "关")
        return {"ok": True, "id": int(folder_id), "privacy": on,
                "hint": ("已设为隐私：打标只走本地模型，问答时内容不会发给外部模型 ✓"
                         if on else "已取消隐私标记")}

    @router.get("/api/folders/{folder_id}/privacy")
    async def get_folder_privacy(request: Request, folder_id: int):
        _need_login(request)
        if store.get_folder(int(folder_id)) is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        return {"ok": True, "id": int(folder_id),
                "privacy": store.get_folder_privacy(int(folder_id))}

    @router.post("/api/caption/queue/start")
    async def caption_queue_start(request: Request, payload: dict = Body(default={})):
        """开始跑 PDF 插图打标队列（后台线程，接口立刻返回）✓

        参数：
          folder_ids  只跑这几个文件夹
          file_ids    只跑这几份 PDF（项目主 2026-10-20："需要指定文件"）✓
          limit       一批取多少（跑完自动接下一批）
          max_hours / until  到点停
        """
        _need_admin_or_local(request)
        from . import caption_run as _cr
        if _cr.is_running():
            return {"ok": False, "message": "已经在跑了", "state": _cr.run_state()}
        folders = [int(x) for x in (payload.get("folder_ids") or [])] or None
        files = [int(x) for x in (payload.get("file_ids") or [])] or None
        try:
            limit = int(payload.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        remote = _remote_caption_cfg()
        vldir = None if remote else _vl_model_dir()
        # 有隐私文件夹时，即使没配外部模型也得能跑（走本地）✓
        if not remote and not vldir:
            raise HTTPException(status_code=400,
                                detail="既没有配外部打标模型，也没找到本机视觉模型")
        res = _cr.start_async(cfg, store, engine, _chat_engine(),
                              folder_ids=folders, file_ids=files,
                              limit=limit or 200,
                              remote=remote, vl_dir=vldir,
                              deadline=_deadline_from(payload), source="manual")
        if res.get("ok"):
            log.info("手动开始 PDF 插图打标（模型：%s，范围：%s）",
                     (remote or {}).get("model") or "本机视觉模型",
                     f"{len(files)} 份文件" if files else
                     (f"{len(folders)} 个文件夹" if folders else "全部待办"))
        return res

    @router.post("/api/caption/queue/pause")
    async def caption_queue_pause(request: Request):
        _need_admin_or_local(request)
        from . import caption_run as _cr
        return _cr.pause()

    @router.post("/api/caption/queue/resume")
    async def caption_queue_resume(request: Request):
        _need_admin_or_local(request)
        from . import caption_run as _cr
        return _cr.resume()

    @router.post("/api/caption/queue/stop")
    async def caption_queue_stop(request: Request):
        _need_admin_or_local(request)
        from . import caption_run as _cr
        return _cr.stop()

    @router.get("/api/vl/status")
    async def vl_status():
        from . import caption as cap
        st = cap.status()
        st["available"] = _scan_chat_models()
        return st

    @router.post("/api/vl/caption")
    async def vl_caption_start(request: Request, payload: dict = Body(default={})):
        """启动后台打标：用 VL 模型给图片生成中文描述，让图片能被文字搜到。

        打标期间嵌入模型会挪到 CPU 腾显存，搜索会慢一点。
        多人：打标会写进索引、还会挪走嵌入模型（所有人受影响）→ 只有管理员 ✓
        """
        _need_admin_or_local(request)
        # ★ 服务器上没有本地视觉模型（项目主 2026-10-21："打标用API可行，
        #   服务器本地模型也变灰"）→ **只拦本地**，外部 API 照常可用 ✓
        #   （一开始写成无条件拒绝了，那会把"用 API 打标"也堵死 ✗）
        if not _can_index_here():
            _want = str(payload.get("model") or payload.get("vl_model") or "")
            if (not _want) or _want in ("local", "本机", "本机VL", "local_vl") or \
                    _want.startswith("local"):
                raise HTTPException(
                    status_code=400,
                    detail="这台是服务器，没有本地视觉模型：请把打标模型选成**外部 API** ✓"
                           "（想用本地模型，请在你自己电脑上打开本机版）")
        from . import caption as cap
        folders = [int(x) for x in (payload.get("folder_ids") or [])] or None
        limit = int(payload.get("limit") or 0)
        win = payload.get("window")
        if win and len(win) == 2:
            win = [int(win[0]), int(win[1])]
        else:
            win = None
        # 用外部模型打标（不走本机 VL，不占显存）
        remote = payload.get("remote") or None
        if remote and not remote.get("base_url"):
            raise HTTPException(status_code=400, detail="外部模型要填地址")
        # 找出 VL 模型目录（不能用配置里的文本模型，它看不到图）
        vl_dir = None
        for m in _scan_chat_models():
            arch = str(m.get("arch") or "")
            if "VLForConditional" in arch or "Vision2Seq" in arch:
                vl_dir = m.get("path")
                break
        if not vl_dir:
            raise HTTPException(status_code=400,
                                detail="没找到视觉语言（VL）模型，无法打标")
        r = cap.start(cfg, store, engine, _chat_engine(), limit=limit,
                      folder_ids=folders, move_embedding=_embedding_to,
                      window=win, vl_dir=vl_dir,
                      vram_limit=int(payload.get("vram_limit") or 0),
                      remote=remote)
        return r

    @router.get("/api/vl/who")
    async def vl_who(request: Request):
        """打标到底会用哪个模型（项目主 2026-10-18 问"怎么确认"，让界面直接显示）✓

        两个来源（和 `/api/vl/start` 的规则一致）：
          · 没勾「用外部模型打标」→ 本机 models/ 里**第一个 VL 架构**的模型 ✓
          · 勾了 → 前端「外部模型」卡里那个（本接口只回报"本机 VL"和"是否要求外部"，
            外部模型的名字由前端从界面偏好里读，避免把 key 之类的东西经后端转手 ✗）
        """
        _need_login(request)
        vl = None
        for m in _scan_chat_models():
            arch = str(m.get("arch") or "")
            if "VLForConditional" in arch or "Vision2Seq" in arch:
                vl = {"name": m.get("name") or "", "path": m.get("path") or "",
                      "arch": arch}
                break
        return {"local_vl": vl,
                "hint": ("不勾「用外部模型打标」→ 用本机这个视觉模型 ✓；"
                         "勾了 → 用「外部模型」卡里选中的那个（那个必须支持看图）")}

    async def vl_skip_list():
        """看被跳过多少张坏图（只含解码失败的）。"""
        try:
            store.ensure_caption_skip()
            n = store.caption_skip_count()
            rows = store._query(
                "SELECT s.file_id, s.reason, s.added, f.name, f.path "
                "FROM caption_skip s LEFT JOIN files f ON f.id=s.file_id "
                "ORDER BY s.added DESC LIMIT 200")
            return {"count": n, "items": [
                {"file_id": r["file_id"], "name": r["name"],
                 "reason": r["reason"], "added": r["added"]} for r in rows]}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/vl/skip/clear")
    async def vl_skip_clear(request: Request):
        """清空跳过名单 —— 换模型/换网络/修好文件后可以重来。

        注意：这是安全的，因为名单里只有「解码就失败」的图；
        网络和模型错误从来没被记进来过。
        """
        _need_admin_or_local(request)
        try:
            store.ensure_caption_skip()
            return {"ok": True, "cleared": store.caption_skip_clear()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/vl/stop")
    async def vl_caption_stop(request: Request):
        _need_admin_or_local(request)
        from . import caption as cap
        cap.stop()
        return {"ok": True}

    # ======================================================== 备份管理
    _BAK_DIR = ROOT / "data" / "index"
    _PENDING = _BAK_DIR / "pending-restore.txt"

    def _bak_info(path: Path) -> dict:
        """读一个备份文件的基本信息和统计（只读，不动原文件）。"""
        import sqlite3 as _sq
        info = {"name": path.name, "size": path.stat().st_size,
                "mtime": path.stat().st_mtime,
                "people": 0, "faces": 0, "assigned": 0, "ok": False}
        try:
            con = _sq.connect(f"file:{path}?mode=ro", uri=True)
            info["people"] = con.execute(
                "SELECT COUNT(*) FROM people WHERE name IS NOT NULL "
                "AND name<>''").fetchone()[0]
            info["faces"] = con.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
            info["assigned"] = con.execute(
                "SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL").fetchone()[0]
            info["ok"] = True
            con.close()
        except Exception as exc:                          # noqa: BLE001
            info["error"] = str(exc)[:80]
        return info

    @router.post("/api/backup")
    async def backup_now(request: Request):
        """立即备份一份索引库（在线备份，不影响使用）。

        多人：备份是整个索引库（含所有人的私有数据）→ 只有管理员 ✓
        """
        _need_admin_or_local(request)
        import sqlite3 as _sq
        import time as _t
        stamp = _t.strftime("%Y%m%d-%H%M%S")
        dst = _BAK_DIR / f"index-backup-{stamp}.db"
        src = _sq.connect(str(store.path))
        out = _sq.connect(str(dst))
        src.backup(out)
        out.close()
        src.close()
        mb = dst.stat().st_size / 1e6
        return {"ok": True, "name": dst.name, "size": dst.stat().st_size,
                "message": f"备份完成：{dst.name}（{mb:.0f} MB）"}

    @router.get("/api/backups")
    async def backups_list(request: Request):
        """列出所有备份版本，按时间从新到旧。

        多人：备份列表里带整库的人数/脸数统计 → 只有管理员 ✓
        """
        _need_admin_or_local(request)
        items = []
        for f in sorted(_BAK_DIR.glob("index-backup-*.db"),
                        key=lambda x: -x.stat().st_mtime):
            items.append(_bak_info(f))
        pending = ""
        if _PENDING.exists():
            pending = _PENDING.read_text(encoding="utf-8").strip()
        return {"backups": items, "pending": pending,
                "total_bytes": sum(x["size"] for x in items)}

    @router.post("/api/backups/restore")
    async def backups_restore(request: Request, payload: dict = Body(...)):
        """标记"下次启动时还原到这个版本"。

        不在这里直接替换 index.db —— 服务正开着，覆盖会损坏数据库。
        重启时由 start.bat 完成替换。
        """
        _need_admin_or_local(request)
        name = str(payload.get("name") or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            raise HTTPException(status_code=400, detail="备份名不合法")
        src = _BAK_DIR / name
        if not src.is_file():
            raise HTTPException(status_code=404, detail="找不到这个备份")
        _PENDING.write_text(name, encoding="utf-8")
        return {"ok": True, "pending": name,
                "note": "已标记。请用 stop.bat 停服务，再双击 search.bat 启动，"
                        "启动时会自动替换并清除标记。"}

    @router.post("/api/backups/cancel-restore")
    async def backups_cancel(request: Request):
        _need_admin_or_local(request)
        if _PENDING.exists():
            _PENDING.unlink()
        return {"ok": True}

    @router.delete("/api/backups/{name}")
    async def backups_delete(request: Request, name: str):
        """删掉一个备份文件。"""
        _need_admin_or_local(request)
        if "/" in name or "\\" in name or ".." in name or not name.endswith(".db"):
            raise HTTPException(status_code=400, detail="名字不合法")
        f = _BAK_DIR / name
        if not f.is_file():
            raise HTTPException(status_code=404, detail="找不到这个备份")
        # 不允许删掉当前正在用的库
        if f.name == "index.db":
            raise HTTPException(status_code=400, detail="不能删正在使用的库")
        size = f.stat().st_size
        f.unlink()
        return {"ok": True, "deleted": name, "freed": size}

    @router.get("/api/index/status")
    async def index_status():
        return {"status": indexer.status(), "stats": store.stats()}

    # ---------------------------------------------------------- 搜索
    @router.get("/api/search")
    async def do_search(
        request: Request,
        q: str = Query("", description="查询内容（可以只给人物，不给关键词）"),
        top_k: int = Query(30, ge=1, le=200),
        folder_ids: str = Query("", description="逗号分隔的文件夹 id"),
        exts: str = Query("", description="逗号分隔的后缀，如 .pdf,.docx"),
        persons: str = Query("", description="逗号分隔的人物 id —— 混合搜索用"),
        person_mode: str = Query("filter", description="filter=只保留含此人的；boost=加分不排除"),
    ):
        if q.strip() and engine.model is None:
            raise HTTPException(status_code=503, detail="模型还没加载完，请稍候")
        ids = [int(x) for x in folder_ids.split(",") if x.strip().isdigit()] or None
        ext_list = [e.strip().lower() for e in exts.split(",") if e.strip()] or None
        pids = [int(x) for x in persons.split(",") if x.strip().isdigit()] or None
        try:
            if pids:
                # 带人物条件 → 走混合搜索（文件名 + 语义 + 人物 融合排序）
                data = searcher.hybrid(q, top_k=top_k, folder_ids=ids, exts=ext_list,
                                       person_ids=pids,
                                       person_mode=person_mode if person_mode in
                                       ("filter", "boost") else "filter")
            else:
                data = searcher.search(q, top_k=top_k, folder_ids=ids, exts=ext_list)
            labels = {f["id"]: FC.describe(f.get("config"))
                      for f in store.list_folders(stats=False)}
            names = {f["id"]: f["path"] for f in store.list_folders(stats=False)}
            # ---- 数据隔离（多人 P2-b）：只返回"我该看到的"文件夹里的命中 ----
            # 匿名 / 普通用户 → 只看共享库（owner_id=0 且 visibility=shared）；
            # 管理员（或还没启用账号体系）→ 不限制。
            # 现有 17 个文件夹全是共享的，所以升级后体感完全不变 ✓
            visible = _visible_folders(request)
            kept = []
            for item in data["results"]:
                fid = int(item.get("folder_id") or 0)
                if visible is not None and fid not in visible:
                    continue
                item["folder_mode"] = labels.get(item.get("folder_id"), "")
                item["folder"] = names.get(item.get("folder_id"), "")
                kept.append(item)
            if visible is not None and len(kept) != len(data["results"]):
                log.debug("搜索隔离：%d 条命中过滤掉 %d 条", len(data["results"]),
                          len(data["results"]) - len(kept))
            data["results"] = kept
            # count 也要跟着改 ✓：不然界面会说"共 4 条"却只显示 2 条
            # （searcher 返回的 count 是**过滤前**的数量，实测对不上）
            data["count"] = len(kept)
            return data
        except InputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("搜索失败")
            raise HTTPException(status_code=500, detail=f"搜索失败：{exc}") from exc

    # ======================================================== 人脸 / 人物
    @router.get("/api/faces/status")
    async def faces_status(request: Request):
        """人脸功能是否可用、索引里有多少张脸。

        ⚠ 参数 request 不能省：过滤可见性要用它取当前用户
        （交接文档铁律 1：改端点忘加 request → 500 ✓）
        """
        from . import faces as face_mod
        ok, why = face_mod.available()
        vis = _visible_folders(request)
        stats = store.face_stats() if vis is None else _face_stats_scoped(vis)
        return {"available": ok, "reason": why,
                "enabled": bool(cfg.face_enabled), **stats}

    @router.get("/api/people")
    async def people_list(request: Request,
                          kind: str = Query("", description="person / object，留空返回全部")):
        vis = _visible_folders(request)
        rows = store.list_people() if vis is None else _people_scoped(vis)
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        # ★ 2026-10-21 项目主："人物标签不用共享出来……共享的照片人物可以不显示，
        #   个人显示个人即可"
        #   → 普通用户只看**完全属于自己的**人物（整组的脸都在我可写文件夹里），
        #     而且数量只算他自己的；管理员照旧看全部 ✓
        w = _writable(request)
        if w is None:
            # 管理员：全站分文件夹计数（本机版筛选也要用，不能只给总数）✓
            _all_fb: dict[int, dict[int, int]] = {}
            for _row in store._query(                                   # noqa: SLF001
                    "SELECT fa.person_id AS pid, f.folder_id AS fid, COUNT(*) AS n "
                    "FROM faces fa JOIN files f ON f.id=fa.file_id "
                    "WHERE fa.person_id>0 GROUP BY fa.person_id, f.folder_id"):
                _all_fb.setdefault(int(_row["pid"]), {})[int(_row["fid"])] = \
                    int(_row["n"])
            _all_img: dict[int, dict[int, int]] = {}
            for _row in store._query(                               # noqa: SLF001
                    "SELECT fa.person_id AS pid, f.folder_id AS fid, "
                    "COUNT(DISTINCT f.id) AS n FROM faces fa "
                    "JOIN files f ON f.id=fa.file_id "
                    "WHERE fa.person_id>0 GROUP BY fa.person_id, f.folder_id"):
                _all_img.setdefault(int(_row["pid"]), {})[int(_row["fid"])] = \
                    int(_row["n"])
            for r in rows:
                r["writable"] = True
                r["faces_by_folder"] = {str(k): v
                                        for k, v in _all_fb.get(int(r["id"]), {}).items()}
                r["files_by_folder"] = {str(k): v
                                        for k, v in _all_img.get(int(r["id"]), {}).items()}
                r["folders"] = sorted(_all_fb.get(int(r["id"]), {}))
        else:
            wset = [int(x) for x in w]
            # 一次查完 (人物 → 涉及哪些文件夹)，别每个人物查一遍
            m: dict[int, set[int]] = {}
            for row in store._query(                                    # noqa: SLF001
                    "SELECT DISTINCT fa.person_id AS pid, f.folder_id AS fid "
                    "FROM faces fa JOIN files f ON f.id=fa.file_id "
                    "WHERE fa.person_id IS NOT NULL AND fa.person_id > 0"):
                m.setdefault(int(row["pid"]), set()).add(int(row["fid"] or 0))
            wset_s = set(wset)
            # 显示口径：**我在自己文件夹里见过这个人**（不是"整组都属于我"）
            # ⚠ 第一版写成 `m[pid] <= wset`（整组都属于我）→ 只显示出 1 个 ✗
            #   因为同一个人往往横跨多个文件夹，那样几乎没人满足
            mine = [r for r in rows if m.get(int(r["id"]), set()) & wset_s]
            # 数量也重算：只算他自己文件夹里的脸（不然会显示"123 张"其实全是别人的）
            cnt: dict[int, int] = {}
            if wset:
                marks = ",".join("?" * len(wset))
                for row in store._query(                                # noqa: SLF001
                        "SELECT fa.person_id AS pid, COUNT(*) AS n FROM faces fa "
                        "JOIN files f ON f.id=fa.file_id "
                        f"WHERE f.folder_id IN ({marks}) "
                        "AND fa.person_id IS NOT NULL GROUP BY fa.person_id",
                        tuple(wset)):
                    cnt[int(row["pid"])] = int(row["n"])
            # ★ 分文件夹计数：卡片上的"N 张"要按当前筛选的文件夹显示，
            #   否则选了「家庭相册」还显示人物总数（项目主三张截图抓出来的）✗
            fb: dict[int, dict[int, int]] = {}
            if wset:
                marks2 = ",".join("?" * len(wset))
                for row in store._query(                                # noqa: SLF001
                        "SELECT fa.person_id AS pid, f.folder_id AS fid, "
                        "COUNT(*) AS n FROM faces fa JOIN files f ON f.id=fa.file_id "
                        f"WHERE f.folder_id IN ({marks2}) AND fa.person_id>0 "
                        "GROUP BY fa.person_id, f.folder_id", tuple(wset)):
                    fb.setdefault(int(row["pid"]), {})[int(row["fid"])] = int(row["n"])
            # ★ 分文件夹的**照片数**（卡片上那个"N 张"用它）
            #   一个文件里同一个人的多张脸只算 1 张照片 → COUNT(DISTINCT file_id)
            fb_img: dict[int, dict[int, int]] = {}
            if wset:
                for row in store._query(                            # noqa: SLF001
                        "SELECT fa.person_id AS pid, f.folder_id AS fid, "
                        "COUNT(DISTINCT f.id) AS n FROM faces fa "
                        "JOIN files f ON f.id=fa.file_id "
                        f"WHERE f.folder_id IN ({marks2}) AND fa.person_id>0 "
                        "GROUP BY fa.person_id, f.folder_id", tuple(wset)):
                    fb_img.setdefault(int(row["pid"]), {})[int(row["fid"])] = \
                        int(row["n"])
            for r in mine:
                r["n_faces"] = cnt.get(int(r["id"]), 0)
                # 能不能改：整组都在我可写范围才算（跨文件夹的只能看不能改）
                r["writable"] = bool(m.get(int(r["id"]), set())) and \
                    m[int(r["id"])] <= wset_s
                # ★ 2026-10-21：给前端做「按文件夹筛选」用 —— 这个人物在我哪些
                #   可写文件夹里出现过（只列我的，不泄漏别人的文件夹 id）✓
                r["folders"] = sorted(m.get(int(r["id"]), set()) & wset_s)
                # {文件夹id: 该文件夹里的脸数}（JSON 的键会是字符串）
                r["faces_by_folder"] = {str(k): v
                                        for k, v in fb.get(int(r["id"]), {}).items()}
                # 照片数：只算他自己的文件夹（原来的 n_files 是全库 ✗）
                r["files_by_folder"] = {str(k): v
                                        for k, v in fb_img.get(int(r["id"]), {}).items()}
                r["n_files"] = sum(r["files_by_folder"].values())
            rows = mine
        return {"people": rows,
                "persons": [r for r in rows if r["kind"] == "person"],
                "objects": [r for r in rows if r["kind"] == "object"]}

    @router.put("/api/people/{person_id}")
    async def people_rename(request: Request, person_id: int, payload: dict = Body(...)):
        rows = store.list_people(stats=False)
        if not any(r["id"] == person_id for r in rows):
            raise HTTPException(status_code=404, detail="没有这个人物")
        _need_people_write(request, person_id)                 # 共享库的人名只读 ✓
        store.set_person_name(person_id, str(payload.get("name", "")),
                              payload.get("kind"))
        cf = payload.get("cover_face")
        if cf:
            _need_face_write(request, int(cf))                 # 封面也得是我能改的那张脸 ✓
            store._conn.execute("UPDATE people SET cover_face=? WHERE id=?",
                                (int(cf), person_id))
            store._conn.commit()
        return {"ok": True}

    @router.get("/api/people/{person_id}/faces")
    async def person_faces(request: Request, person_id: int, limit: int = Query(60),
                           offset: int = Query(0)):
        """列出某个人的所有脸，用来手动挑一张当封面。

        按「清晰度」排序：置信度高 x 脸大 的排前面，好挑。
        多人隔离：只列**我该看到的**文件夹里的脸 ✓（同一个人可能既有共享照片
        也有私有照片，所以不能整组放行或整组拦掉 ✗）。
        """
        n = max(1, min(int(limit), 400))
        vis = _visible_folders(request)
        sql = ("SELECT f.id, f.prob, f.x1, f.y1, f.x2, f.y2, fi.name AS fname, "
               "       fi.path AS fpath "
               "FROM faces f JOIN files fi ON fi.id=f.file_id "
               "WHERE f.person_id=?")
        params: list = [person_id]
        if vis is not None:
            if not vis:
                return {"person_id": person_id, "count": 0, "faces": []}
            ph, vals = _ph(vis)
            sql += f" AND fi.folder_id IN ({ph})"
            params += vals
        # ★ 2026-10-21 项目主："不满意可以重新换一批" → 支持翻页
        #   先数总数（前端要显示"第 1-90 张 / 共 800 张"并决定按钮要不要灰）
        total = int(store._query(
            "SELECT COUNT(*) AS n FROM faces f JOIN files fi ON fi.id=f.file_id "
            "WHERE f.person_id=?" + (f" AND fi.folder_id IN ({ph})" if vis is not None else ""),
            tuple([person_id] + (vals if vis is not None else [])))[0]["n"])
        off = max(0, int(offset))
        sql += (" ORDER BY (f.prob * (f.x2-f.x1) * (f.y2-f.y1)) DESC LIMIT ? OFFSET ?")
        params.extend([n, off])
        rows = store._query(sql, tuple(params))

        # 把缩略图直接做成 data URI 内嵌。
        # 为什么：/api/faces/N/thumb 这个独立图片请求会被某些浏览器插件
        # （广告拦截、隐私保护）拦掉，表现就是弹窗里只有文字没有图，
        # 而且控制台还不一定报错，极难排查。内嵌就没有这个问题。
        import base64 as _b64
        out = []
        for r in rows:
            item = {"id": int(r["id"]),
                    "prob": round(float(r["prob"] or 0), 3),
                    "w": int((r["x2"] or 0) - (r["x1"] or 0)),
                    "file": r["fname"]}
            # 缩略图落盘缓存：现读 NAS 上的原图很慢（60 张要 10 秒以上），
            # 缓存后第二次打开就是瞬间的。
            cache = ROOT / "data" / "thumbs" / f"{int(r['id'])}.jpg"
            try:
                if not cache.exists():
                    from PIL import Image as _Image
                    box = (max(0, int(r["x1"] or 0)), max(0, int(r["y1"] or 0)),
                           int(r["x2"] or 0), int(r["y2"] or 0))
                    if box[2] > box[0] and box[3] > box[1]:
                        im = _Image.open(Path(r["fpath"])).convert("RGB")
                        crop = im.crop(box)
                        crop.thumbnail((96, 96), _Image.LANCZOS)
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        crop.save(str(cache), format="JPEG", quality=76)
                if cache.exists():
                    item["thumb"] = ("data:image/jpeg;base64,"
                                     + _b64.b64encode(cache.read_bytes()).decode("ascii"))
            except Exception:                             # noqa: BLE001
                pass                                      # 拿不到就还是用独立请求
            out.append(item)
        return {"person_id": person_id, "count": len(out), "faces": out,
                "total": total, "offset": off, "limit": n}

    @router.delete("/api/faces/{face_id}/drop")
    async def face_drop(request: Request, face_id: int):
        """彻底删掉一条人脸记录 —— 用于「这根本就不是脸」的误检。

        和 /person 的区别：
          · /person 只是把脸从某人身上摘下来，记录还在，聚类会再捡回来
          · /drop   把记录删掉，永远不会再出现

        多人：只有这张脸所在的文件夹归你（或你是管理员）才能删 ✓
        """
        _need_face_write(request, face_id)
        row = store._query("SELECT person_id FROM faces WHERE id=?", (face_id,))
        if not row:
            raise HTTPException(status_code=404, detail="没有这张脸")
        pid = row[0]["person_id"]
        with store._conn:
            store._conn.execute("DELETE FROM faces WHERE id=?", (face_id,))
            if pid:
                store._conn.execute(
                    "UPDATE people SET n_faces=(SELECT COUNT(*) FROM faces "
                    "WHERE person_id=?) WHERE id=?", (pid, pid))
                cur = store._query("SELECT cover_face, n_faces FROM people WHERE id=?",
                                   (pid,))
                if cur and (int(cur[0]["cover_face"] or 0) == face_id
                            or not cur[0]["n_faces"]):
                    nxt = store._query(
                        "SELECT id FROM faces WHERE person_id=? "
                        "ORDER BY (prob*(x2-x1)*(y2-y1)) DESC LIMIT 1", (pid,))
                    store._conn.execute("UPDATE people SET cover_face=? WHERE id=?",
                                        (int(nxt[0]["id"]) if nxt else None, pid))
        return {"ok": True, "dropped": face_id, "was_person": pid}

    @router.delete("/api/faces/{face_id}/person")
    async def face_detach(request: Request, face_id: int):
        """把一张人脸从它所属的人物身上摘下来（归错了时用）。

        摘掉后它变成「未归属」，可以在界面上点「重新聚类」重新进组。
        多人：同 /drop，只能动自己文件夹里的脸 ✓
        """
        _need_face_write(request, face_id)
        row = store._query("SELECT person_id FROM faces WHERE id=?", (face_id,))
        if not row:
            raise HTTPException(status_code=404, detail="没有这张脸")
        old = row[0]["person_id"]
        store._conn.execute("UPDATE faces SET person_id=NULL WHERE id=?", (face_id,))
        if old:
            store._conn.execute(
                "UPDATE people SET n_faces=(SELECT COUNT(*) FROM faces "
                "WHERE person_id=?) WHERE id=?", (old, old))
            # 如果封面就是这张，换一张
            cur = store._query("SELECT cover_face FROM people WHERE id=?", (old,))
            if cur and int(cur[0]["cover_face"] or 0) == face_id:
                nxt = store._query(
                    "SELECT id FROM faces WHERE person_id=? "
                    "ORDER BY (prob*(x2-x1)*(y2-y1)) DESC LIMIT 1", (old,))
                store._conn.execute("UPDATE people SET cover_face=? WHERE id=?",
                                    (int(nxt[0]["id"]) if nxt else None, old))
        store._conn.commit()
        return {"ok": True, "face_id": face_id, "was_person": old}

    @router.put("/api/people/{person_id}/cover")
    async def person_set_cover(request: Request, person_id: int, payload: dict = Body(...)):
        """把某张脸设成这个人物的封面（头像）。"""
        _need_people_write(request, person_id)                 # 人不是我的 → 403 ✓
        face_id = int(payload.get("face_id") or 0)
        if not face_id:
            raise HTTPException(status_code=400, detail="要指定 face_id")
        _need_face_write(request, face_id)
        row = store._query("SELECT person_id FROM faces WHERE id=?", (face_id,))
        if not row:
            raise HTTPException(status_code=404, detail="没有这张脸")
        if int(row[0]["person_id"] or 0) != person_id:
            raise HTTPException(status_code=400,
                                detail="这张脸不属于该人物，不能设为封面")
        store._conn.execute("UPDATE people SET cover_face=? WHERE id=?",
                            (face_id, person_id))
        store._conn.commit()
        return {"ok": True, "person_id": person_id, "cover_face": face_id}

    @router.post("/api/people/{person_id}/detach-all")
    async def person_detach_all(request: Request, person_id: int):
        """整组解散：把这个人物名下的所有脸都变成「未归属」。

        用于「这一组整个都认错了」的情况 —— 解散后点「重新聚类」重新分。
        名字本身也一并清掉，免得留一个空组。
        多人：整组只能在完全属于我的文件夹里解散 ✓
        """
        _need_people_write(request, person_id)
        n = store._query("SELECT COUNT(*) c FROM faces WHERE person_id=?",
                         (person_id,))[0]["c"]
        with store._conn:
            store._conn.execute("UPDATE faces SET person_id=NULL WHERE person_id=?",
                                (person_id,))
            store._conn.execute("DELETE FROM people WHERE id=?", (person_id,))
        return {"ok": True, "detached": int(n)}

    @router.post("/api/people/dedup")
    async def people_dedup(request: Request):
        """把所有同名的人物组合并成一组（保留脸最多的那组）。

        同名多组会让「按人物筛选」莫名其妙地漏掉一半照片，
        而且很难被发现。这个接口用来一键清理。

        多人：这是**整库**重写（会把共享库的人名/分组一起动）→ 只有管理员 ✓
        """
        _need_admin_or_local(request)
        rows = store._query(
            "SELECT name, GROUP_CONCAT(id) ids FROM people "
            "WHERE name IS NOT NULL AND name <> '' "
            "GROUP BY name HAVING COUNT(*) > 1")
        merged = 0
        with store._conn:
            for r in rows:
                ids = [int(x) for x in str(r["ids"]).split(",")]
                sized = []
                for g in ids:
                    n = store._query(
                        "SELECT COUNT(*) c FROM faces WHERE person_id=?", (g,)
                    )[0]["c"]
                    sized.append((int(n), g))
                sized.sort(key=lambda x: -x[0])
                keep = sized[0][1]
                for _n, g in sized[1:]:
                    store._conn.execute(
                        "UPDATE faces SET person_id=? WHERE person_id=?", (keep, g))
                    store._conn.execute("DELETE FROM people WHERE id=?", (g,))
                    merged += 1
            store._conn.execute(
                "UPDATE people SET n_faces=(SELECT COUNT(*) FROM faces "
                "WHERE person_id = people.id)")
            store._conn.execute("DELETE FROM people WHERE n_faces = 0")
        return {"ok": True, "names": len(rows), "merged": merged}

    @router.post("/api/people/merge-similar")
    async def people_merge_similar(request: Request, payload: dict = Body(default={})):
        """把「未命名的整组」并进最像的「已命名人物」。

        针对的问题是：聚类门槛一严，同一个人会被拆成好几组。
        逐个脸归类太慢，按组做质心比较快得多。

        参数：
          threshold  组质心至少要这么像（默认 0.68，比 assign 松一点，
                     因为组质心比单张脸稳定）
          dry        只报告不执行（默认 true，界面先预览）

        多人：整库合并 + 会读全部人脸向量 → 只有管理员 ✓
        """
        _need_admin_or_local(request)
        import numpy as np

        thr = float(payload.get("threshold") or 0.68)
        dry = payload.get("dry", True)
        cache = store.load_face_matrix()
        if not cache:
            return {"ok": False, "detail": "没有人脸向量"}

        people = {int(x["id"]): x for x in store.list_people(stats=False)}
        # 每个组的质心（用该组人脸向量的平均）
        cents: dict[int, "np.ndarray"] = {}
        counts: dict[int, int] = {}
        for dim, g in cache.items():
            V, ids = g["vectors"], g["face_ids"]
            fmap = store._query(
                "SELECT id, person_id FROM faces WHERE id IN "
                f"({','.join('?' * len(ids))})", [int(i) for i in ids])
            owner = {int(r["id"]): r["person_id"] for r in fmap}
            bucket: dict[int, list] = {}
            for k, fid in enumerate(ids):
                pid = owner.get(int(fid))
                if pid:
                    bucket.setdefault(int(pid), []).append(k)
            for pid, ks in bucket.items():
                if len(ks) < 2:
                    continue
                v = V[ks].mean(axis=0)
                n = float(np.linalg.norm(v))
                if n > 1e-6:
                    cents[pid] = (v / n).astype(np.float32)
                    counts[pid] = len(ks)
            break                       # 用维度最高的那份缓存就够

        named = {pid: (ppl["name"] or "").strip()
                 for pid, ppl in people.items() if (ppl["name"] or "").strip()}
        unnamed = [pid for pid in cents if pid not in named]
        named_ids = [pid for pid in named if pid in cents]
        if not named_ids or not unnamed:
            return {"ok": True, "merged": 0, "detail": "没有可合并的组"}

        C = np.stack([cents[p] for p in named_ids])
        plan = []
        for pid in unnamed:
            v = cents[pid]
            sims = C @ v
            k = int(sims.argmax())
            best = float(sims[k])
            if best >= thr:
                plan.append({"from": pid, "to": named_ids[k],
                             "name": named[best and named_ids[k]],
                             "sim": round(best, 3),
                             "faces": counts.get(pid, 0)})
        plan.sort(key=lambda x: -x["faces"])
        if dry:
            return {"ok": True, "dry": True, "threshold": thr,
                    "candidates": len(plan),
                    "faces": sum(x["faces"] for x in plan),
                    "preview": plan[:25]}

        with store._conn:
            for x in plan:
                store._conn.execute(
                    "UPDATE faces SET person_id=? WHERE person_id=?",
                    (x["to"], x["from"]))
                store._conn.execute("DELETE FROM people WHERE id=?", (x["from"],))
            store._conn.execute(
                "UPDATE people SET n_faces=(SELECT COUNT(*) FROM faces "
                "WHERE person_id = people.id)")
            store._conn.execute(
                "UPDATE people SET cover_face=(SELECT id FROM faces "
                "WHERE person_id = people.id "
                "ORDER BY (prob*(x2-x1)*(y2-y1)) DESC LIMIT 1) "
                "WHERE n_faces > 0")
            store._conn.execute("DELETE FROM people WHERE n_faces = 0")
        return {"ok": True, "dry": False, "threshold": thr,
                "merged": len(plan),
                "faces": sum(x["faces"] for x in plan),
                "preview": plan[:25]}

    @router.post("/api/people/people-merge")
    async def people_merge(request: Request, payload: dict = Body(...)):
        """把几组合并成一组（同一个人被拆开时用）。

        多人：合并会改到被并入那几组的人名/封面 → 两组的脸都必须归我 ✓
        """
        target = int(payload.get("target", 0))
        sources = [int(x) for x in payload.get("sources", []) if int(x) != target]
        if not target or not sources:
            raise HTTPException(status_code=400, detail="需要 target 和 sources")
        _need_people_write(request, target, *sources)
        with store._conn:
            for s in sources:
                store._conn.execute("UPDATE faces SET person_id=? WHERE person_id=?",
                                    (target, s))
                store._conn.execute("DELETE FROM people WHERE id=?", (s,))
            store._conn.execute(
                "UPDATE people SET n_faces=(SELECT COUNT(*) FROM faces WHERE person_id=?) "
                "WHERE id=?", (target, target))
        return {"ok": True, "merged": len(sources)}

    @router.post("/api/people/rebuild")
    async def people_rebuild(request: Request, payload: dict = Body(default={})):
        """把所有人脸重新聚类成「人物」组。已有命名会按多数归属沿用。

        多人：重建会**重写全部**人物分组（含共享库）→ 只有管理员 ✓
        """
        _need_admin_or_local(request)
        thr = float(payload.get("threshold") or cfg.face_cluster_threshold)
        res = store.rebuild_people(threshold=thr)
        return {"ok": True, **res, "people": len(store.list_people(stats=False))}

    @router.post("/api/people/assign")
    async def people_assign(request: Request, payload: dict = Body(default={})):
        """按已命名的人脸中心，把每张脸归类（未命名的脸也能归到最像的人）。

        多人：同 rebuild，整库动作 → 只有管理员 ✓
        """
        _need_admin_or_local(request)
        thr = float(payload.get("threshold") or cfg.face_assign_threshold)
        res = store.assign_people(threshold=thr)
        return {"ok": True, **res}

    @router.get("/api/faces/{face_id}/thumb")
    async def face_thumb(face_id: int, w: int = 160):
        """从原图里裁出这张脸，作为人物头像。"""
        rows = store._query("SELECT * FROM faces WHERE id=?", (face_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="没有这张脸")
        row = rows[0]
        info = store.get_file(int(row["file_id"]))
        if info is None:
            raise HTTPException(status_code=404, detail="原文件不在索引里")
        path = Path(info["path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"原文件找不到了：{path.name}")
        try:
            from PIL import Image, ImageOps
            with Image.open(path) as im:
                img = ImageOps.exif_transpose(im).convert("RGB")
                x1, y1, x2, y2 = row["x1"], row["y1"], row["x2"], row["y2"]
                bw, bh = max(x2 - x1, 1.0), max(y2 - y1, 1.0)
                mx, my = bw * 0.28, bh * 0.28          # 留点边，头像好看些
                box = (max(0, int(x1 - mx)), max(0, int(y1 - my)),
                       min(img.width, int(x2 + mx)), min(img.height, int(y2 + my)))
                crop = img.crop(box)
            w = max(48, min(int(w), 800))
            crop.thumbnail((w, w), Image.LANCZOS)
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=88)
            return Response(content=buf.getvalue(), media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=86400"})
        except Exception as exc:                      # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"裁头像失败：{exc}") from exc

    # ======================================================== 对话模型选择
    def _embedding_to(device: str) -> str:
        """把嵌入模型在显卡和 CPU 之间搬（运行时搬，不用重启）。

        为什么需要：3060 Ti 只有 8GB，嵌入模型占 2.3GB。
        用 3B/4B 对话模型时放不下，就把嵌入模型挪到 CPU ——
        代价只是查询编码慢 1-2 秒，相比生成时间可忽略。
        """
        try:
            if engine.model is None:
                return "嵌入模型还没加载，跳过"
            cur = str(getattr(engine, "device", "") or "")
            if cur.startswith(device):
                return f"嵌入模型已在{'CPU' if device == 'cpu' else '显卡'}"
            engine.model.to(device)
            engine.device = device
            if device == "cpu":
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:                         # noqa: BLE001
                    pass
            return f"嵌入模型已移到{'CPU' if device == 'cpu' else '显卡'}"
        except Exception as exc:                          # noqa: BLE001
            return f"搬移嵌入式模型失败：{exc}"

    def _weights_gb(path: Path) -> float:
        try:
            ws = list(path.glob("*.safetensors")) + list(path.glob("*.bin"))
            return sum(w.stat().st_size for w in ws) / 1e9
        except OSError:
            return 0.0

    def _auto_fit_vram(chat_gb: float) -> list[str]:
        """按对话模型的体积，自动安排嵌入模型放哪。

        规则：
          显存放不下（留 900MB 给 KV 缓存）→ 嵌入模型搬 CPU
          放得下且有余量（>2.5GB）        → 嵌入模型搬回显卡
        """
        notes: list[str] = []
        free = _gpu_free_mb()
        if free is None:
            return ["读不到显存信息，跳过自动调整"]

        on_gpu = str(getattr(engine, "device", "") or "").startswith("cuda")
        need_mb = chat_gb * 1024 + 900

        if free < need_mb and on_gpu and engine.model is not None:
            notes.append(_embedding_to("cpu"))
            free = _gpu_free_mb() or free
            notes.append(f"腾出后显存余 {free} MB")
        elif free > need_mb + 2500 and (not on_gpu) and engine.model is not None:
            notes.append("显存够用，" + _embedding_to("cuda"))
        else:
            notes.append(f"显存 {free} MB，够放（需要约 {int(need_mb)} MB），不用调整")
        return notes

    def _scan_chat_models() -> list[dict]:
        """列出 models\\ 下所有可用的对话模型。"""
        base = ROOT / "models"
        out = []
        if not base.is_dir():
            return out
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            if not (d / "config.json").exists():
                continue
            weights = list(d.glob("*.safetensors")) + list(d.glob("*.bin"))
            if not weights:
                continue
            size = sum(w.stat().st_size for w in weights)
            cfg_txt = ""
            try:
                cfg_txt = (d / "config.json").read_text(encoding="utf-8")
            except OSError:
                pass
            # 只有架构里带 ForCausalLM 的才算对话模型 ——
            # 嵌入模型（embeddinggemma-2）也会被扫到，但它不能用来对话
            kind = "对话" if "ForCausalLM" in cfg_txt else "其它"
            import json as _js
            try:
                _a = _js.loads(cfg_txt).get("architectures") or []
            except (ValueError, TypeError):
                _a = []
            out.append({"name": d.name, "path": str(d), "gb": round(size / 1e9, 2),
                        "kind": kind, "arch": ",".join(_a), "is_chat": kind == "对话"})
        return out

    # chat 引擎句柄（放在模块里，避免每次请求重建）
    _chat_state: dict = {}

    def _chat_engine():
        if "e" not in _chat_state:
            from .chat import ChatEngine
            saved = store.get_meta("chat_model") or ""
            saved_path = Path(saved) if saved else None
            if saved_path and not saved_path.is_absolute():
                saved_path = ROOT / saved_path
            if saved_path and not (saved_path / "config.json").exists():
                saved_path = None
            _chat_state["e"] = ChatEngine(cfg, model_dir=saved_path)
        return _chat_state["e"]

    @router.post("/api/upload-image")
    async def upload_image(payload: dict = Body(...)):
        """接收前端上传的图片（base64），返回规整后的 base64。

        为什么要过一趟服务端：HEIC/TIFF 浏览器自己显示不了，
        而且太大的图直接塞进模型会爆显存。这里统一解码 + 缩到 1024。
        """
        import base64 as _b64
        import io as _io

        from PIL import Image as _Im
        raw = str(payload.get("data") or "").split(",")[-1]
        if not raw:
            raise HTTPException(status_code=400, detail="没有图片数据")
        try:
            im = _Im.open(_io.BytesIO(_b64.b64decode(raw))).convert("RGB")
        except Exception as exc:                              # noqa: BLE001
            raise HTTPException(status_code=415,
                                detail=f"这个图片打不开：{exc}") from exc
        if max(im.size) > 1024:
            r = 1024 / max(im.size)
            im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))))
        buf = _io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
        return {"ok": True, "data": "data:image/jpeg;base64," + b64,
                "w": im.width, "h": im.height,
                "bytes": len(buf.getvalue())}

    @router.get("/api/chat/models")
    async def chat_models():
        eng = _chat_engine()
        cur = str(eng.model_dir)
        items = _scan_chat_models()
        for it in items:
            it["current"] = (Path(it["path"]) == Path(cur))
            # 标出这个模型能不能看图 —— 前端据此给「视觉」标签，
            # 并决定上传图片时该提示用户切到哪个模型。
            arch = str(it.get("arch") or "")
            it["vl"] = ("VLForConditional" in arch or "Vision2Seq" in arch
                        or "vl" in str(it.get("name", "")).lower())
        ok, why = eng.available()
        return {"models": items, "current": cur, "available": ok,
                "reason": why, "device": eng.device,
                "loaded": eng.is_loaded,
                "gpu": _gpu_free_mb()}

    @router.post("/api/chat/model")
    async def chat_set_model(request: Request, payload: dict = Body(...)):
        """切换对话模型。会先卸下当前模型腾显存，下次提问时加载新的。

        多人：换模型影响所有人的问答 → 只有管理员 ✓
        """
        _need_admin_or_local(request)
        want = str(payload.get("path") or payload.get("name") or "").strip()
        if not want:
            raise HTTPException(status_code=400, detail="要指定模型")
        target = Path(want)
        if not target.is_absolute():
            target = ROOT / (want if want.startswith("models") else f"models/{want}")
        if not (target / "config.json").exists():
            raise HTTPException(status_code=400, detail=f"不是有效的模型目录：{target}")
        eng = _chat_engine()
        eng.switch_to(target)
        store.set_meta("chat_model", str(target))
        ok, why = eng.available()
        # 自动把嵌入模型挪到合适的位置（显存够就放显卡，不够就放 CPU）
        notes = _auto_fit_vram(_weights_gb(target))
        return {"ok": True, "path": str(target), "available": ok, "reason": why,
                "vram": _gpu_free_mb(), "vram_notes": notes,
                "message": f"已切换到 {target.name}，下次提问时加载"}

    @router.get("/api/chat/vram")
    async def chat_vram():
        """给界面用的显存信息（够不够放下当前模型）。"""
        eng = _chat_engine()
        size_gb = None
        d = eng.model_dir
        if d.is_dir():
            ws = list(d.glob("*.safetensors")) + list(d.glob("*.bin"))
            if ws:
                size_gb = round(sum(w.stat().st_size for w in ws) / 1e9, 2)
        return {"free_mb": _gpu_free_mb(), "model_gb": size_gb,
                "device": eng.device, "loaded": eng.is_loaded}


    # ======================================================== 知识库问答

    def _kb_chunks(question: str, top_k: int, folder_ids: list[int] | None):
        """检索知识库，返回可喂给模型的片段列表。

        三个关键处理（都是实测踩出来的）：
        1. **先保证来源分散** —— 第一轮每个文件只取 1 片。之前一上来就允许
           每文件 2 片，结果 5 个名额只覆盖 2 个文件，答案只能看到片面内容。
        2. **过滤噪声片段** —— 网页剪藏的 markdown 里混着大量 base64、
           SVG、%编码串、图片链接参数。实测有一条 URL 编码串被当成"正文"
           喂给模型，模型只能说"资料里没有提到"。
        3. **按相似度重排** —— 第一轮捡回来的片段按分数排序后再截断。
        """
        vector = engine.encode([question], prompt_name="SearchQuery")[0]
        # 知识库是「用文字问、用文字答」，所以**只排除图片**。
        #
        # 注意这里必须用黑名单而不是白名单：正文片段的 kind 不止 'text'，
        # PDF 是 'pdf'、Excel 是 'xlsx'、图片描述是 'caption'。
        # 之前用白名单 ('text','caption') 把 PDF 和 Excel 的正文一起排除了，
        # 结果家谱 PDF 里的人名怎么都搜不到。用黑名单以后新增 docx/pptx 也不会漏。
        #
        # 图片 chunk 必须排除：它的向量是给「以图搜图」用的，text 是空的或
        # 哈希文件名，拿来和文字查询比会算出虚高的相似度，把有用的片段挤掉。
        hits = store.search(vector, top_k=max(top_k * 6, 60),
                            folder_ids=folder_ids, literal=question,
                            exclude_kinds=("image",))

        # ---- 关键词兜底：按字面在片段正文里再搜一遍 ----
        # 向量搜索对专有名词不敏感（人名、编号、书名、命令），
        # 而这些恰恰是知识库里最需要精确命中的东西。
        # 多人 ⑤：优先走 **FTS5**（逐字索引 + 短语查询，子串语义、<1ms ✓），
        # 索引没建起来 / 查询切不出词时才退回老的 `LIKE` 全表扫（65~90ms/词 ✗）。
        try:
            kws = _query_keywords(question)
            if kws:
                seen = {int(h["id"]) for h in hits}
                extra: list[dict] = []
                fts_rows: list[dict] = []
                if getattr(store, "fts_state", {}).get("ok"):
                    # 每个词单独查（词之间是"或"关系 ✓）——
                    # 别拼成一条整句查询：那样要求片段同时含所有词，等于 0 命中 ✗
                    for kw in kws[:6]:
                        for hr in store.fts_chunks(kw, limit=40,
                                                   folder_ids=folder_ids or None,
                                                   exclude_kinds=("image",)):
                            if len((hr.get("snippet") or "")) <= 8:
                                continue
                            fts_rows.append({
                                "id": int(hr["file_id"]), "name": hr.get("name"),
                                "path": hr.get("path"), "dir": hr.get("dir"),
                                "folder_id": hr["folder_id"], "ext": "",
                                "size": 0, "score": 0.90,
                                "snippet": (hr.get("snippet") or "")[:300],
                                "_fts_text": (hr.get("snippet") or "")[:900],
                                "_literal": True,
                            })
                elif kws:
                    for kw in kws[:4]:
                        for row in store._query(
                                "SELECT ch.file_id, ch.text, ch.kind, f.name, "
                                "       f.path, f.dir, f.folder_id, f.ext, f.size "
                                "FROM chunks ch JOIN files f ON f.id=ch.file_id "
                                "WHERE ch.text LIKE ? AND ch.kind<>'image' "
                                "  AND LENGTH(ch.text)>8 "
                                "LIMIT 40", ("%" + kw + "%",)):
                            if folder_ids and row["folder_id"] not in folder_ids:
                                continue
                            fts_rows.append({
                                "id": int(row["file_id"]), "name": row["name"],
                                "prefer_text": kw, "path": row["path"],
                                "dir": row["dir"], "folder_id": row["folder_id"],
                                "ext": row["ext"], "size": row["size"],
                                "score": 0.90, "snippet": (row["text"] or "")[:300],
                                "_literal": True,
                            })
                for item in fts_rows:
                    fid2 = int(item["id"])
                    if fid2 in seen:
                        # 文件已经在候选里了，但它可能是靠向量召回的，
                        # 那样就不知道关键词落在哪一片，take() 只翻前 10 片
                        # 会漏掉（家谱 PDF 的「某人甲」在 seq=17）。
                        # 所以给它补上 prefer_text，让 take() 去精确捞那一片。
                        for _h in hits:
                            if int(_h["id"]) == fid2 and not _h.get("prefer_text"):
                                if item.get("_fts_text"):
                                    _h["prefer_text_fts"] = item["_fts_text"]
                                    _h["score"] = max(float(_h.get("score") or 0), 0.90)
                                elif item.get("prefer_text"):
                                    _h["prefer_text"] = item["prefer_text"]
                                break
                        continue
                    seen.add(fid2)
                    extra.append(item)
                if extra:
                    hits = hits + extra
                    log.info("知识库关键词兜底（%s）：%s → 补 %d 个文件",
                             "FTS5" if getattr(store, "fts_state", {}).get("ok") else "LIKE",
                             ",".join(kws[:4]), len(extra))
        except Exception as exc:                              # noqa: BLE001
            log.warning("关键词兜底失败：%s", exc)

        per_file: dict[int, int] = {}
        chunks: list[dict] = []

        def take(item, cap: int) -> int:
            """从某个文件里再取最多 cap 片未被取过的、非噪声的片段。

            关键词兜底召回的条目要特殊处理：先把它命中的那一片捞出来。
            否则 ORDER BY seq LIMIT 10 只翻前 10 片，
            而关键词可能落在第 17 片（家谱 PDF 就是这样），永远提不到。
            """
            got = 0
            rows = []
            pref_fts = item.get("prefer_text_fts")
            if pref_fts:
                # FTS5 直接给了命中的那一片正文 ✓（不用再 LIKE 捞一次）
                rows.append({"text": str(pref_fts), "kind": item.get("_fts_kind")})
            pref = item.get("prefer_text")
            if pref and "\n" not in str(pref)[:200]:
                rows += list(store._query(
                    "SELECT text, kind FROM chunks "
                    "WHERE file_id=? AND text LIKE ? AND kind<>'image' "
                    "ORDER BY LENGTH(text) LIMIT 3",
                    (item["id"], "%" + str(pref) + "%")))
            rows += list(store._query(
                "SELECT text, kind FROM chunks WHERE file_id=? AND text<>'' "
                "ORDER BY seq LIMIT 10", (item["id"],)))
            for r in rows:
                if got >= cap:
                    break
                text = (r["text"] or "").strip()
                if _kb_is_noise(text):
                    continue
                chunks.append({"file_id": item["id"], "name": item["name"],
                               "path": item["path"], "dir": item["dir"],
                               # ★ 2026-10-20：**必须带 folder_id**
                               #   「🔒 隐私文件夹」靠它判断能不能发给外部模型；
                               #   之前这里没带 → 过滤条件永远命中 0 条 →
                               #   隐私内容照样被发出去 ✗（实测抓出来的）
                               "folder_id": int(item.get("folder_id") or 0),
                               "score": item["score"], "text": text[:900]})
                got += 1
            per_file[item["id"]] = per_file.get(item["id"], 0) + got
            return got

        # ---- 关键词命中的片段直接注入 ----
        # 精确匹配优先于语义相似：只要正文里出现了问的那个词，
        # 那一片就必须进结果，不和向量结果抢名额。
        # （否则道教、神仙壁纸那些向量分更高的片段会把家谱 PDF 挤掉。）
        try:
            for kw in _query_keywords(question)[:3]:
                for row in store._query(
                        "SELECT ch.file_id, ch.text, f.name, f.path, f.dir, "
                        "       f.folder_id, f.ext, f.size "
                        "FROM chunks ch JOIN files f ON f.id=ch.file_id "
                        "WHERE ch.text LIKE ? AND ch.kind<>'image' "
                        "  AND LENGTH(ch.text)>8 "
                        "ORDER BY LENGTH(ch.text) LIMIT 2",
                        ("%" + kw + "%",)):
                    if folder_ids and row["folder_id"] not in folder_ids:
                        continue
                    body = (row["text"] or "").strip()
                    if not body or _kb_is_noise(body):
                        continue
                    if any(c["file_id"] == row["file_id"]
                           and body[:60] in c["text"] for c in chunks):
                        continue
                    chunks.append({
                        "file_id": row["file_id"], "name": row["name"],
                        "path": row["path"], "dir": row["dir"],
                        "score": 0.95,          # 精确命中，排最前
                        "text": body[:1500],    # 名单类片段给足长度，别截断
                    })
            if chunks:
                log.info("知识库精确命中 %d 片（已直接注入）", len(chunks))
        except Exception as exc:                              # noqa: BLE001
            log.warning("关键词注入失败：%s", exc)

        # 关键：处理前必须按分数排序！
        # 否则向量结果（60+ 条）排在前面，关键词兜底追加的条目在队尾，
        # 而下面的循环装够 top_k 就 break —— 队尾的永远轮不到，
        # 分数给多高都没用（实测踩过：家谱 PDF 被召回却始终进不了结果）。
        hits.sort(key=lambda h: -float(h.get("score") or 0))

        # 第一轮：每个文件只 1 片，尽量铺开
        for item in hits:
            if len(chunks) >= top_k:
                break
            if per_file.get(item["id"], 0) >= 1:
                continue
            take(item, 1)

        # 第二轮：还不够就允许每个文件再来 1 片（上限 2）
        if len(chunks) < top_k:
            for item in hits:
                if len(chunks) >= top_k:
                    break
                if per_file.get(item["id"], 0) >= 2:
                    continue
                take(item, 1)

        chunks.sort(key=lambda c: -c["score"])
        # 精确命中的片段保底留下（最多不超过一半名额），
        # 其余按分数取，避免专有名词被语义结果淹没。
        exact = [c for c in chunks if c["score"] >= 0.95]
        rest = [c for c in chunks if c["score"] < 0.95]
        keep_exact = exact[: max(1, top_k // 2)]
        out = keep_exact + rest[: max(0, top_k - len(keep_exact))]
        out.sort(key=lambda c: -c["score"])
        return out

    @router.get("/api/chat/status")
    async def chat_status():
        """对话模型是否就绪（本机模型 + 界面里配好的外部模型）。"""
        eng = _chat_engine()
        ok, why = eng.available()
        remote = _prefs_remote()
        return {"available": ok, "reason": why,
                "dir": str(eng.model_dir), "device": eng.device if ok else "",
                "note": eng.note, "emb_device": str(getattr(engine, "device", "")),
                "gpu": _gpu_free_mb(),
                "kb_folders": len(_kb_folder_ids()),
                "top_k": cfg.chat_top_k,
                # 界面据此提示：本机没模型时，用外部模型照样能问答
                "remote_configured": bool(remote),
                "remote_model": (remote or {}).get("model", ""),
                "remote_url": (remote or {}).get("base_url", "")}

    @router.post("/api/chat/unload")
    async def chat_unload(request: Request):
        """卸下对话模型腾显存 —— 影响所有人的问答 → 只有管理员 ✓"""
        _need_admin_or_local(request)

        """把对话模型从显卡上摘下来腾显存。"""
        _chat_engine().unload()
        return {"ok": True}

    def _prefs_remote() -> dict | None:
        """从界面偏好里读出「外部模型」配置（eg2-rm*）。

        为什么后端也要读：服务器上**没有本机对话模型**（没显卡，跑 1.5B 太慢），
        问答只能靠外部 API。配置本来就存在界面偏好里，读出来就能在用户忘了勾
        「用外部模型回答」时也自动用上 —— 之前这里没读，导致没本机模型就直接
        503"对话模型不可用"，配了外部 API 也用不了。
        """
        try:
            prefs = store.prefs_all()
        except Exception:                                    # noqa: BLE001
            return None
        url = str(prefs.get("eg2-rmurl") or "").strip()
        model = str(prefs.get("eg2-rmmodel") or "").strip()
        if not url or not model:
            return None
        hdr = None
        raw = str(prefs.get("eg2-rmhdr") or "").strip()
        if raw:
            try:
                hdr = json.loads(raw)
            except Exception:                                # noqa: BLE001
                hdr = None

        def _num(key: str, default: int) -> int:
            try:
                return int(float(prefs.get(key) or default))
            except (TypeError, ValueError):
                return default

        return {"base_url": url, "model": model,
                "api_key": str(prefs.get("eg2-rmkey") or ""),
                "headers": hdr if isinstance(hdr, dict) else None,
                "max_side": _num("eg2-rmside", 448),
                "max_tokens": _num("eg2-rmtok", 800)}

    @router.post("/api/chat")
    async def chat_ask(request: Request, payload: dict = Body(...)):
        """知识库问答：只检索标了 use_kb 的文件夹，回答带引用编号。"""
        import time as _t

        from .chat import build_messages, cite_numbers, trim_chunks

        q = str(payload.get("q") or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="问题不能为空")

        # ★ 2026-10-22 项目主："把 Dify 也接进「知识库问答」页，加一个选择开关" ——
        #   以前这一页只查本项目知识库，Dify 只有「聊天」页能用（两页行为不一致 ✗）。
        #   现在前端多一个「☁ Dify」开关，勾上就多查一次 Dify，
        #   结果和本库片段一起进提示词：本库标 [K1]…，Dify 标 [D1]… ✓
        use_dify = bool(payload.get("dify"))

        # ---- 先决定用哪个模型（三段语义，勾选框必须仍然有效）----
        #   ① 勾了「用外部模型回答」→ 前端把 remote 一起发过来，一定用外部
        #   ② 没勾 + 本机对话模型可用 → 用本机
        #   ③ 没勾 + 本机没有对话模型（服务器没显卡就是这样）→ 自动改用界面里
        #      配好的外部模型，并在回答里带一句说明，免得用户以为勾选框坏了
        explicit = payload.get("remote")
        # ★ 2026-10-22：前端选了「服务器公用 / 别人共享」那几套时只回传 `api:N`，
        #   密钥留在服务端 —— 这里按同一套规则（app/apimodels.py）解析成 remote ✓
        _pick = str(payload.get("pick") or "").strip()
        if _pick.startswith("api:"):
            try:
                from . import apimodels as _am

                _u0 = _who(request) or {}
                _it = _am.by_index(store, int(_u0.get("id") or 0),
                                   int(_pick.split(":", 1)[1]),
                                   str(_u0.get("name") or ""))
                if _it:
                    explicit = {"base_url": _it.get("base_url"),
                                "model": _it.get("model"),
                                "api_key": _it.get("api_key") or "",
                                # headers 在偏好里是 JSON 字符串 → 必须解析成 dict，
                                # 否则 vlm_client 对它调 .items() 会报
                                # "外部模型调用失败：'str' object has no attribute 'items'" ✗
                                "headers": _am.headers_of(_it)}
            except Exception as _exc:                            # noqa: BLE001
                log.warning("知识库问答解析模型选择失败（%s）：%s", _pick, _exc)
        use_explicit = bool(isinstance(explicit, dict) and explicit.get("base_url")
                            and explicit.get("model"))
        prefs_remote = _prefs_remote()
        remote = explicit if use_explicit else None
        auto_note = ""
        eng = _chat_engine()
        if not use_explicit:
            ok, why = eng.available()
            if ok:
                remote = None                      # ② 本机可用 → 就用本机
            elif prefs_remote:
                remote = prefs_remote              # ③ 本机没有 → 自动外部
                auto_note = ("本机没有对话模型，已自动改用外部模型"
                             f"（{prefs_remote.get('model')}）")
            else:
                raise HTTPException(
                    status_code=503,
                    detail=f"对话模型不可用：{why}。"
                           f"也可以在「设置 → 外部模型」里填一个外部模型"
                           f"（服务器上没显卡时就用它，不需要本机模型）。")
        use_remote = bool(remote)
        if engine.model is None:
            raise HTTPException(status_code=503, detail="嵌入模型还没加载完")

        kb_ids = [int(x) for x in (payload.get("folder_ids") or [])] or _kb_folder_ids()
        # ---- 知识库隔离（多人 P2-b）：范围收窄到"这个用户能看到的" ----
        # 默认的全部知识库文件夹、以及前端显式点名的，都要过这一道 ✓
        # （不然可以手写 folder_ids 去引用别人的私有文件夹 ✗）
        kb_ids = _scope_kb(kb_ids, request)
        if not kb_ids:
            raise HTTPException(
                status_code=400,
                detail=_no_kb_hint())

        top_k = max(1, min(int(payload.get("top_k") or cfg.chat_top_k), 20))
        started = _t.perf_counter()
        chunks = _kb_chunks(q, top_k, kb_ids)
        # ★ 隐私文件夹（2026-10-20 项目主）：用外部模型回答时，**它的片段一条都不发出去** ✓
        #   项目主："我的隐私文件统统放在里面，这样设置更方便" + "用外部模型也搜索不到"
        #   注意顺序：必须在 trim_chunks **之前**摘掉，否则它们会白占上下文预算 ✓
        privacy_hidden = 0
        privacy_note = ""
        if use_remote:
            priv = private_folder_ids(store)
            if priv:
                kept = [c for c in chunks if int(c.get("folder_id") or 0) not in priv]
                privacy_hidden = len(chunks) - len(kept)
                chunks = kept
                if privacy_hidden:
                    privacy_note = (
                        f"有 {privacy_hidden} 条资料来自「🔒 隐私文件夹」，"
                        f"按设置**没有发给外部模型**。"
                        f"想让它参与回答，请用本机模型提问。")
                    log.info("隐私保护：%d 条片段未发送给外部模型", privacy_hidden)
        # ★ 控制上下文长度（2026-10-19）：不裁的话会超过模型上限 8192 token ✗
        #   → 问答很慢 → 界面心跳被拖断 → 服务误判"界面关了"而自杀
        #   在**这里**裁（而不是拼提示词时）→ 「来源」列表和喂给模型的保持一致 ✓
        chunks = trim_chunks(chunks)

        # ---- ③b Dify 知识库（可选，2026-10-22 加）----
        #   ★ 2026-10-22：改成"自己的 + 别人共享给我的"（app/difyshare.py）
        #     共享规则：private（默认）/ all（所有人）/ to（名单里的人）
        dify = None
        if use_dify:
            try:
                from . import difyshare

                _u = _who(request) or {}
                _dl = difyshare.usable(store, int(_u.get("id") or 0),
                                       str(_u.get("name") or ""))
                dify = difyclient.retrieve_multi(_dl, q, 3)
            except Exception as exc:                             # noqa: BLE001
                log.warning("知识库问答：Dify 检索异常 %s", exc)
                dify = {"ok": False, "results": [], "used": [],
                        "errors": [f"Dify 检索异常：{exc}"]}
        dify_ctx = (difyclient.as_context_multi(dify)
                    if (dify and dify.get("ok")) else "")

        if not chunks and not dify_ctx:
            if privacy_hidden:
                return {"answer": "这个问题只匹配到「🔒 隐私文件夹」里的内容。"
                                  "为安全起见一条都没有发送给外部模型；"
                                  "请改用本机模型提问，或换个问法。",
                        "sources": [], "chunks": 0, "cited": [],
                        "privacy_hidden": privacy_hidden,
                        "note": privacy_note,
                        "dify": dify,
                        "elapsed": round(_t.perf_counter() - started, 2)}
            _miss = ("知识库里没有检索到相关内容。先把要问的文件夹标成"
                     "「知识库」并重建索引，再试。")
            if use_dify and dify and not dify.get("ok"):
                _errs = "；".join(dify.get("errors") or []) or "没有可用的 Dify 库"
                _miss = f"本项目知识库和 Dify 都没检索到内容（Dify：{_errs}）。"
            elif use_dify:
                _miss = "本项目知识库和 Dify 都没检索到相关内容。换个问法试试。"
            return {"answer": _miss,
                    "sources": [], "chunks": 0,
                    "dify": dify,
                    "elapsed": round(_t.perf_counter() - started, 2)}

        msgs = build_messages(q, chunks, payload.get("history") or [])
        if dify_ctx:
            # 拼在**同一条 user 消息**里（和本库片段并排），编号各成一套 ✓
            msgs[-1]["content"] = (str(msgs[-1].get("content") or "")
                                   + "\n\n" + dify_ctx)
            msgs[0]["content"] = (str(msgs[0].get("content") or "")
                                  + "\n另外还给了 Dify 知识库的片段，"
                                    "引用时标 [D1] [D2]（和本库的 [1] [2] 各自独立）。")

        def _make_sources(chunk_list: list[dict], cited_set) -> list[dict]:
            """构造「来源」列表。

            多人 ⑤ 补丁（2026-10-16）：每条来源带上**真的出现在这个片段里的查询关键词**
            （`hit_terms`，长的优先）✓ —— 从来源点开文件时，预览就拿这个词去
            PDF 里定位第几页；拿整个问题（「李四是谁」）是定位不到的 ✗
            （实测现象：预览只会提示"页面上没有…的字面文字"）。
            **抽成一个函数是因为原来这段造了两遍**（外部模型一条、本机模型一条），
            只改一处就会漏另一处 ✗ —— 服务器上走外部模型那条，当时就漏了。
            """
            kws = sorted(_query_keywords(q), key=lambda w: -len(w))
            out = []
            for i, c in enumerate(chunk_list, 1):
                text = c["text"]
                out.append({
                    "n": i, "file_id": c["file_id"], "name": c["name"],
                    "path": c["path"], "dir": c["dir"],
                    "score": c["score"], "text": text[:300],
                    "hit_terms": [w for w in kws if w in text][:4],
                    "cited": i in cited_set,
                })
            return out

        # 用外部模型回答（不占本机显存，理解力更强；服务器上就靠它）
        if use_remote:
            from .vlm_client import chat_once as _co
            try:
                ans = _co(str(remote["base_url"]), str(remote["model"]),
                          msgs, max_tokens=max(int(cfg.chat_max_tokens), 1600),
                          api_key=str(remote.get("api_key") or ""),
                          extra_headers=remote.get("headers") or None)
            except Exception as exc:                          # noqa: BLE001
                raise HTTPException(status_code=502,
                                    detail=f"外部模型调用失败：{exc}") from exc
            # ★ 2026-10-20：有隐私内容被拦时，**在回答正文里说明** ✓
            #   只放在 note 字段里的话，用户只看答案 → 不知道有资料没发出去 ✗
            #   （实测：拦下了 2 条，但回答里一点提示都没有，看起来像"资料里没有"）
            if privacy_note:
                ans = f"> 🔒 {privacy_note}\n\n{ans}"
            cited2 = cite_numbers(ans)
            sources2 = _make_sources(chunks, cited2)
            return {"answer": ans, "sources": sources2,
                    "chunks": len(chunks), "cited": cited2,
                    "device": "remote", "vl": False, "n_images": 0,
                    "note": auto_note,
                    "privacy_hidden": privacy_hidden,
                    "privacy_note": privacy_note,
                    "dify": dify,
                    "elapsed": round(_t.perf_counter() - started, 2)}

        # 可选：带图片提问（VL 模型才看得到图；文本模型会忽略）
        imgs = []
        for b64 in (payload.get("images") or [])[:4]:
            try:
                import base64 as _b64
                import io as _io

                from PIL import Image as _Im
                raw = str(b64).split(",")[-1]
                im = _Im.open(_io.BytesIO(_b64.b64decode(raw))).convert("RGB")
                if max(im.size) > 1024:
                    r = 1024 / max(im.size)
                    im = im.resize((int(im.width * r), int(im.height * r)))
                imgs.append(im)
            except Exception as exc:                          # noqa: BLE001
                log.warning("图片解析失败：%s", exc)

        out = eng.chat(msgs, max_new_tokens=cfg.chat_max_tokens,
                       temperature=cfg.chat_temperature,
                       images=imgs or None)
        cited = cite_numbers(out["answer"])
        sources = _make_sources(chunks, cited)
        return {
            "answer": out["answer"], "sources": sources,
            "chunks": len(chunks), "cited": cited,
            "device": out.get("device"), "tokens": out.get("completion_tokens"),
            "vl": bool(out.get("vl")), "n_images": len(imgs),
            "dify": dify,
            "elapsed": round(_t.perf_counter() - started, 2),
        }

    # ======================================================== 知识库
    def _kb_folder_ids() -> list[int]:
        """哪些文件夹被标成了「知识库」。"""
        out = []
        for row in store.list_folders(stats=False):
            if FolderConfig(cfg, row.get("config")).use_kb:
                out.append(int(row["id"]))
        return out

    @router.get("/api/kb/folders")
    async def kb_folders(request: Request):
        # 多人隔离：只列可见的（私有文件夹的路径和"是不是知识库"都不该给别人看 ✓）
        vis = _visible_folders(request)
        rows = [r for r in store.list_folders(stats=False)
                if vis is None or int(r["id"]) in vis]
        # 「能不能改」也要给（前端据此把编辑控件置灰 ✓）——
        # 普通用户看到管理员的知识库是**只读**的，别让点了保存才吃 403 ✗
        try:
            from .multiuser import writable_folder_ids as _writable

            # ★ 2026-10-21：本机（建库机）不限制 —— 本机的普通用户和管理员一样
            #   能看能改本机所有文件夹（项目主："只差能不能管服务器"）✓
            wr = None if _local_open(cfg.local_strict) else _writable(cfg.db_path, _who(request))
            wset = None if wr is None else {int(x) for x in wr}
        except Exception as exc:                                  # noqa: BLE001
            log.warning("取可写文件夹失败，按「都不能改」处理：%s", exc)
            wset = set()

        kb, other = [], []
        for row in rows:
            fc = FolderConfig(cfg, row.get("config"))
            item = {"id": int(row["id"]), "path": row["path"], "use_kb": fc.use_kb,
                    "writable": wset is None or int(row["id"]) in wset}
            (kb if fc.use_kb else other).append(item)
        return {"kb": kb, "other": other}

    @router.get("/api/folders/{folder_id}/exclude-candidates")
    def exclude_candidates(request: Request, folder_id: int,
                           limit: int = Query(40000), max_seconds: float = 15.0):
        """列出这个文件夹里「有哪些扩展名 / 有哪些子目录」，供勾选排除。

        递归统计扩展名（这是最常排的），子目录只列两层（再深就不好选了）。
        多人：这是读 + 会去扫目录（很吃 NAS）→ 看不到的文件夹直接 403 ✓

        ⚠ 2026-10-18：和 preview-exclude 一样，这里**必须是同步 def** ✓
          以前是 `async def` 且**两次全量遍历**（每个子目录 os.walk + 整根 os.walk），
          NAS 上要跑很久 → 事件循环被占死 → "正在读取文件夹…"卡住 + 服务像崩了一样 ✗
          现在：① 交给线程池 ② 最多跑 max_seconds 秒，超时返回已扫部分并标 time_up ✓
        """
        vis = _visible_folders(request)
        if vis is not None and int(folder_id) not in vis:
            raise HTTPException(status_code=403, detail="这个文件夹对你是私有的，看不到")
        row = store.get_folder(folder_id)
        if row is None:
            raise HTTPException(status_code=404, detail="没有这个文件夹")
        root = Path(row["path"])
        if not root.is_dir():
            raise HTTPException(status_code=400, detail=f"目录不存在：{root}")
        # ⚠ 这个函数里要按"文件夹自己的排除规则"过滤子目录，所以得构造 fcfg ✓
        #   （2026-10-18：漏了这一行 → NameError → 整个接口 500 ✗）
        fcfg = FolderConfig(cfg, row.get("config"))

        exts: dict[str, int] = {}
        dirs: list[dict] = []
        scanned = 0
        truncated = False
        time_up = False
        deadline = time.monotonic() + max(3.0, float(max_seconds))

        # 一层：直接子目录（带文件数）
        try:
            top = sorted(os.scandir(root), key=lambda e: e.name.lower())
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"读不了目录：{exc}") from exc

        for entry in top:
            if time.monotonic() > deadline:
                truncated = True
                time_up = True
                break
            if not entry.is_dir(follow_symlinks=False):
                continue
            # ★ 和 /api/fs/list 一样：排除了的目录不该出现在"从文件夹里选"的列表里 ✓
            if entry.name.startswith("$") or entry.name.lower() in cfg.ignore_dirs:
                continue
            try:
                if fcfg is not None and fcfg.excluded(Path(entry.path), root, True):
                    continue
            except Exception:                                     # noqa: BLE001
                pass
            n = 0
            for _dirpath, _dirnames, filenames in os.walk(entry.path):
                n += len(filenames)
                if n > 20000 or time.monotonic() > deadline:
                    break
            dirs.append({"name": entry.name, "rel": entry.name, "files": n})
            scanned += n          # 子目录阶段也计入 ✓ 否则超时时会显示"扫了 0 个"✗

        # 递归：扩展名统计
        for _dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() > deadline:
                truncated = True
                time_up = True
                break
            dirnames[:] = [d for d in dirnames if not d.startswith("$")]
            for fn in filenames:
                scanned += 1
                if scanned > limit:
                    truncated = True
                    break
                ext = Path(fn).suffix.lower()
                key = ext or "(无扩展名)"
                exts[key] = exts.get(key, 0) + 1
            if truncated:
                break

        top_ext = sorted(exts.items(), key=lambda kv: -kv[1])[:60]
        return {
            "root": str(root),
            "scanned": scanned,
            "truncated": truncated,
            "time_up": time_up,
            "max_seconds": max_seconds,
            "exts": [{"ext": k, "count": v} for k, v in top_ext],
            "dirs": dirs[:80],
            "dirs_total": len(dirs),
        }

    @router.post("/api/folders/{folder_id}/preview-exclude")
    def preview_exclude(request: Request, folder_id: int,
                        payload: dict = Body(default={})):
        """预览排除规则的效果 —— 用界面上当前的（可能还没保存的）规则试算。

        多人：这是"改配置"链路上的一步（会扫目录）→ 只有能改这个文件夹的人能用 ✓

        ⚠ 2026-10-18：这里**必须是同步 def**（不是 async def）✓✓
          以前是 `async def` 里**直接跑同步遍历** ✗ → 遍历 NAS 要 20 秒，
          这 20 秒里**整个事件循环被占死** ✗：心跳、健康检查、其它请求全部排不上队 →
          项目主看到的就是"点预览排除 → 服务崩了 / 再也不响应"✗
          改成同步 def 后，FastAPI 会把它丢到**线程池**里跑 ✓
          事件循环不受影响 ✓ 界面还能继续操作 ✓
        """
        _need_folder_write(request, folder_id)
        row = store.get_folder(folder_id)
        if row is None:
            raise HTTPException(status_code=404, detail="没有这个文件夹")
        root = Path(row["path"])
        if not root.is_dir():
            raise HTTPException(status_code=400, detail=f"目录不存在：{root}")

        # 界面上传了 config 就用它（能试算没保存的规则），否则用已保存的
        override = payload.get("config")
        fcfg = (FolderConfig(cfg, override if override is not None
                             else row.get("config")))
        try:
            return indexer.preview_exclude(root, fcfg)
        except Exception as exc:                      # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"预览失败：{exc}") from exc

    @router.post("/api/kb/retrieve")
    async def kb_retrieve(request: Request, payload: dict = Body(...)):
        """检索预览：只查「知识库」文件夹里的文字片段。

        这是做问答前必须先过的一关 —— 如果这里检索出来的片段
        根本答不了你的问题，那接上对话模型也是白搭。
        """
        q = str(payload.get("q") or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="问题不能为空")
        if engine.model is None:
            raise HTTPException(status_code=503, detail="模型还没加载完")
        top_k = max(1, min(int(payload.get("top_k") or 8), 40))
        kb_ids = [int(x) for x in (payload.get("folder_ids") or [])] or _kb_folder_ids()
        # ---- 知识库隔离（多人 P2-b）：范围收窄到"这个用户能看到的" ----
        # 默认的全部知识库文件夹、以及前端显式点名的，都要过这一道 ✓
        # （不然可以手写 folder_ids 去引用别人的私有文件夹 ✗）
        kb_ids = _scope_kb(kb_ids, request)
        if not kb_ids:
            return {"query": q, "count": 0, "chunks": [], "kb_folders": [],
                    "hint": _no_kb_hint()}

        vector = engine.encode([q], prompt_name="SearchQuery")[0]
        hits = store.search(vector, top_k=max(top_k * 4, 40), folder_ids=kb_ids,
                            literal=q)
        # ---- 关键词通道（FTS5，多人 ⑤）：把正文里精确命中问句词组的片段直接捞出来 ✓
        # 老代码这里只按**文件名**加关键词分（片段正文里的专有名词完全靠向量，容易漏 ✗）
        fts_hits = store.fts_chunks(q, limit=max(top_k * 3, 24), folder_ids=kb_ids,
                                    exclude_kinds=("image",)) if kb_ids else []
        if fts_hits:
            have = {int(h["id"]) for h in hits}
            added_new = 0
            for h in fts_hits:
                fid = int(h["file_id"])
                if fid in have:
                    # 文件已经在向量候选里了，但**片段**可能不是命中那一片
                    # （家谱 PDF 的命中在第 17 片）→ 把命中片段也补进来 ✓
                    # 末尾的 (file_id, text[:60]) 去重会处理重复 ✓
                    pass
                else:
                    added_new += 1
                hits.append({
                    "id": fid, "name": h.get("name") or "",
                    "path": h.get("path") or "", "dir": h.get("dir") or "",
                    "score": 0.9, "_fts_text": h.get("snippet") or "",
                })
            log.info("知识库检索：FTS5 关键词补 %d 个片段（其中 %d 个文件是向量没召回的）",
                     len(fts_hits), added_new)

        # 把文件级结果展开成片段级，方便直接喂给对话模型
        chunks: list[dict] = []
        for item in hits:
            if item.get("_fts_text"):
                # FTS 命中的那一片直接放最前面 ✓（它就是问句词组的出处）
                chunks.append({
                    "file_id": item["id"], "name": item["name"],
                    "path": item["path"], "dir": item["dir"],
                    "score": item["score"], "kind": "text",
                    "text": str(item["_fts_text"])[:800], "channel": "fts",
                })
        for item in hits:
            rows = store._query(
                "SELECT id, text, kind FROM chunks WHERE file_id=? AND text<>'' "
                "ORDER BY seq LIMIT 6", (item["id"],))
            for r in rows:
                text = (r["text"] or "").strip()
                if len(text) < 20:
                    continue
                chunks.append({
                    "file_id": item["id"], "name": item["name"],
                    "path": item["path"], "dir": item["dir"],
                    "score": item["score"], "kind": r["kind"],
                    "text": text[:800],
                })
        chunks.sort(key=lambda c: -c["score"])
        seen, uniq = set(), []
        for c in chunks:
            key = (c["file_id"], c["text"][:60])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(c)
        return {"query": q, "count": len(uniq[:top_k]), "chunks": uniq[:top_k],
                "kb_folders": kb_ids,
                "total_chars": sum(len(c["text"]) for c in uniq[:top_k])}

    # ======================================================== Dify 外部知识库
    # 为什么不把数据导进 Dify：Dify 不接受外部向量，导进去就得用它自己的模型
    # 重新切分 + 嵌入一遍（这套库 320 万字正文 + 7587 条图片描述，重算既费时
    # 又会丢掉本项目的融合检索：向量 + 字面关键词注入 + 图片描述）。
    # 所以只把「检索」这一步暴露给 Dify：Dify 负责编排和对话，知识还在本机。

    def _dify_key() -> str:
        return (cfg._get("dify", "api_key", "") or "").strip()

    def _dify_check_key(request: Request) -> None:
        """Dify 会用 Authorization: Bearer <密钥> 调用，密钥就是 config.ini [dify] api_key。"""
        want = _dify_key()
        if not want:
            raise HTTPException(
                status_code=403,
                detail="还没设置密钥：请在 config.ini 的 [dify] 段填上 api_key，"
                       "重启服务后，在 Dify 那边填同一个值。")
        auth = request.headers.get("authorization") or ""
        got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not got or not secrets.compare_digest(got, want):
            raise HTTPException(status_code=401, detail="密钥不对（Bearer 里的值和 [dify] api_key 不一致）")

    def _dify_folder_ids(knowledge_id: Any) -> list[int]:
        """knowledge_id 决定查哪些知识库文件夹（Dify 让用户自己填这个 ID）：
             · 空 / all / *    → 所有标了「作为知识库」的文件夹
             · 35 或 35,36     → 按文件夹 id
             · obsidian        → 路径里含这个词的知识库文件夹
           匹配不到就退回「全部」，免得一个拼写错误让 Dify 永远检不到东西。"""
        kb = _kb_folder_ids()
        kid = str(knowledge_id or "").strip()
        if not kid or kid.lower() in ("all", "*"):
            return kb
        wanted_ids: list[int] = []
        parts: list[str] = []
        for raw in kid.replace(";", ",").replace(" ", ",").split(","):
            raw = raw.strip()
            if not raw:
                continue
            if raw.isdigit():
                wanted_ids.append(int(raw))
            else:
                parts.append(raw.lower())
        picked = [fid for fid in kb if fid in wanted_ids]
        if parts:
            for row in store.list_folders(stats=False):
                fid = int(row["id"])
                if fid not in kb:
                    continue
                path = str(row["path"]).lower()
                if any(p in path for p in parts):
                    picked.append(fid)
        picked = sorted(set(picked))
        if not picked:
            log.info("Dify 传来的 knowledge_id=%r 没匹配到知识库文件夹，按全部处理", kid)
            return kb
        return picked

    @router.get("/dify/info")
    async def dify_info(request: Request):
        """自查用：密钥对不对、有哪些知识库、模型好了没。
        （Dify 只用 /dify/retrieval，这个接口是给人排错用的）"""
        _dify_check_key(request)
        folders = []
        for fid in _kb_folder_ids():
            row = store.get_folder(fid)
            if row:
                n = store._query(
                    "SELECT COUNT(*) c FROM chunks ch JOIN files f ON f.id=ch.file_id "
                    "WHERE f.folder_id=? AND ch.kind<>'image'", (fid,))[0]["c"]
                folders.append({"id": fid, "path": row["path"], "chunks": int(n)})
        return {"ok": True, "service": "宇之语·灵犀", "kb_folders": folders,
                "model_loaded": engine.model is not None, "device": str(engine.device),
                "knowledge_id_hint": "填 all（全部知识库），或文件夹 id / 路径关键词"}

    @router.post("/dify/retrieval")
    async def dify_retrieval(request: Request, payload: dict = Body(...)):
        """Dify「外部知识库」的检索接口，按 Dify 官方规范实现。

        Dify 侧：设置 → 外部知识库 API（地址填本服务根地址、密钥填 [dify] api_key）
                 → 创建知识库时选「外部知识库」→ External Knowledge ID 填 all 或文件夹 id
        返回：{"records": [{"content", "score", "title", "metadata"}]}
        """
        _dify_check_key(request)
        query = str(payload.get("query") or "").strip()
        if not query:
            return {"records": []}                     # 空查询给空数组，别报错
        setting = payload.get("retrieval_setting") or {}
        try:
            top_k = int(setting.get("top_k") or cfg.chat_top_k)
        except (TypeError, ValueError):
            top_k = cfg.chat_top_k
        top_k = max(1, min(top_k, 40))
        try:
            threshold = float(setting.get("score_threshold") or 0.0)
        except (TypeError, ValueError):
            threshold = 0.0
        if engine.model is None:
            raise HTTPException(status_code=503, detail="嵌入模型还没加载完，稍后再试")

        folder_ids = _dify_folder_ids(payload.get("knowledge_id"))
        if not folder_ids:
            return {"records": []}

        chunks = _kb_chunks(query, top_k, folder_ids)
        # 打分归一化：本项目的分数会超过 1（实测看到 1.185，字面命中还给 0.90/0.95），
        # 而 Dify 约定 score 是 0~1，并且拿它做阈值过滤和排序 ——
        # 直接透传会让「相似度阈值」这个功能彻底失效。这里按本次最高分归一，
        # 相对次序不变，1.0 表示"本次最相关的那一条"。
        best = max([float(c.get("score") or 0.0) for c in chunks] or [0.0])
        records: list[dict] = []
        for c in chunks:
            raw = float(c.get("score") or 0.0)
            score = round(min(1.0, max(0.0, raw / best)), 4) if best > 0 else 0.0
            if score < threshold:
                continue
            records.append({
                "content": c.get("text") or "",
                "score": score,
                "title": c.get("name") or "",
                # 注意：metadata 必须是对象，不能是 null —— Dify 规范里写明
                # null 会让它的检索管线报错。
                "metadata": {
                    "path": c.get("path") or "",
                    "dir": c.get("dir") or "",
                    "file_id": int(c.get("file_id") or 0),
                    "source": "宇之语·灵犀",
                },
            })
        log.info("Dify 检索：%r → %d 条（knowledge_id=%r, top_k=%d, 阈值=%.2f）",
                 query[:40], len(records), payload.get("knowledge_id"), top_k, threshold)
        return {"records": records}

    # ======================================================== 索引库热重载
    @router.post("/api/admin/reload-db", include_in_schema=False)
    async def admin_reload_db(request: Request):
        """把索引库换成新文件，让服务重新打开它 —— **不用重启**。

        为什么需要：本机（有显卡）负责建库，服务器负责提供服务。本机把新的
        index.db 传上来之后，如果只是覆盖文件，服务还盯着**旧 inode**，读到的
        永远是旧数据；而重启一次要 145 秒（CPU 上模型加载很慢）。所以这里：
          ① 关掉旧连接（释放旧文件）
          ② 如果旁边有 index.db.new，就原子替换过去（顺带清掉 -wal/-shm，
             否则旧 WAL 会被套用到新文件上，那是真会弄坏库的）
          ③ 重新开一份 Store + 重建 Searcher（它持有 store 引用，必须一起换）

        安全：只允许本机调用（同步脚本通过 ssh 在服务器上 curl 本机地址）。
        """
        nonlocal store, searcher
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(status_code=403, detail="只允许本机调用")
        db_dir = cfg.db_path.parent
        new_file = db_dir / "index.db.new"
        swapped = False
        if new_file.is_file() and new_file.stat().st_size > 4096:
            # 先替换文件再关连接：Linux 上替换一个"正在被打开"的文件是安全的
            # （旧 inode 会一直活到最后一个句柄关闭），这样中间不会出现
            # "没有库可用"的窗口。
            for suffix in ("-wal", "-shm"):
                junk = db_dir / ("index.db" + suffix)
                try:
                    junk.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("清不掉 %s：%s", junk.name, exc)
            os.replace(new_file, cfg.db_path)                 # 同盘替换，原子
            swapped = True
            log.info("索引库已替换：%s", cfg.db_path)
        try:
            store.close()
        except Exception as exc:                              # noqa: BLE001
            log.warning("关闭旧索引库连接时出错（继续）：%s", exc)
        # 关键：**所有**持有者一起换（搜索路由/设置路由/Indexer/app.state），
        # 只换这里这一份的话，别处会继续用已经关闭的旧连接（实测设置页 500）。
        from .dbreload import notify as _notify_reload

        new_store = Store(cfg)
        rebinds = _notify_reload(new_store)
        store = new_store
        searcher = Searcher(cfg, new_store, engine)
        counts = {}
        for table in ("files", "chunks", "faces", "people", "folders"):
            try:
                counts[table] = int(store._query(
                    f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])
            except Exception:                                 # noqa: BLE001
                counts[table] = -1
        log.info("索引库热重载完成（swapped=%s，换连接=%s）：%s", swapped, rebinds, counts)
        return {"ok": True, "swapped": swapped, "rebinds": rebinds,
                "db": str(cfg.db_path), "size_mb": round(cfg.db_path.stat().st_size / 1048576, 1),
                "counts": counts}

    @router.post("/api/faces/backfill")
    async def faces_backfill(request: Request, payload: dict = Body(default={})):
        """只给「已索引但没有人脸数据」的图片补提人脸。

        不重新编码、不重跑语义向量 —— 只跑人脸提取，比全部重建索引快得多。
        一次处理一批（默认 20 张），前端循环调到 remaining 为 0。

        多人：补提会往索引里写人脸数据 —— 指定了文件夹就得归我，
        没指定（=全部文件夹）只有管理员能做 ✓
        """
        from . import faces as face_mod

        if not cfg.face_enabled:
            raise HTTPException(status_code=400,
                                detail="人脸功能没开启：请把 config.ini 的 face.enabled 设为 yes")
        ok, why = face_mod.available()
        if not ok:
            raise HTTPException(status_code=503, detail=f"人脸功能不可用：{why}")

        limit = max(1, min(int(payload.get("limit") or 20), 200))
        fid = int(payload.get("folder_id") or 0) or None      # 0/缺省 = 全部文件夹
        if fid is None:
            _need_admin_or_local(request)
        else:
            _need_folder_write(request, fid)
        if payload.get("reset"):
            store.reset_face_marker(fid)

        pending = store.faces_pending(limit, fid)
        did = found = 0
        last = 0
        for row in pending:
            last = int(row["id"])
            got = indexer._extract_faces(last, Path(row["path"]))
            did += 1
            if got > 0:
                found += got
        if last:
            store.set_face_marker(last, fid)
        vis = _visible_folders(request)
        stats = store.face_stats() if vis is None else _face_stats_scoped(vis)
        return {"processed": did, "faces": found, "folder_id": fid,
                "remaining": store.faces_remaining(fid),
                "note": indexer._face_note, **stats}

    @router.post("/api/faces/search")
    async def faces_search(request: Request,
                           top_k: int = Query(60, ge=1, le=300),
                           threshold: float = Query(0.0),
                           folder_ids: str = Query("")):
        """以图搜人：上传一张参考照片，找出同一个人（或最像的人）的所有照片。

        请求体直接放图片字节（Content-Type: image/*），避免依赖 multipart。
        """
        from . import faces as face_mod

        ok, why = face_mod.available()
        if not ok:
            raise HTTPException(status_code=503, detail=f"人脸功能不可用：{why}")
        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="没有收到图片数据")
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as exc:                      # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"这不是能识别的图片：{exc}") from exc

        vec = face_mod.engine().embed(img)
        if vec is None:
            raise HTTPException(status_code=422, detail="这张图里没检出人脸，换一张更清晰的正面照")

        thr = float(threshold) if threshold > 0 else cfg.face_search_threshold
        hits = store.search_faces(vec, top_k=top_k, threshold=thr)
        # ---- 数据隔离（多人 P2-b 收尾）：以图搜人也只在"我该看到的"文件夹里找 ----
        vis = _visible_folders(request)
        keep = None if vis is None else {int(x) for x in vis}

        # 顺便告诉前端「这最像谁」
        # 注意：这里也必须只看可见范围 ✓ —— 否则上传一张私有库里的人脸照，
        # 接口会把「这最像 XXX」的名字回给你 ✗（等于隔着隔离墙报姓名）
        guess = None
        people = [p for p in store.list_people(stats=False) if p["name"].strip()]
        if people:
            if keep is None:
                rows = store._query(
                    "SELECT person_id, vec, dim FROM faces WHERE person_id IS NOT NULL")
            else:
                rows = store._query(
                    "SELECT fa.person_id, fa.vec, fa.dim, f.folder_id FROM faces fa "
                    "JOIN files f ON f.id = fa.file_id WHERE fa.person_id IS NOT NULL")
            best: dict[int, float] = {}
            v = np.asarray(vec, dtype=np.float32)
            for r in rows:
                if keep is not None and int(r["folder_id"] or 0) not in keep:
                    continue
                if int(r["dim"]) != len(v):
                    continue
                iv = _from_blob(r["vec"])
                s = float(iv @ v)
                pid = int(r["person_id"])
                if s > best.get(pid, -1):
                    best[pid] = s
            if best:
                pid = max(best, key=best.get)
                name = next((p["name"] for p in people if p["id"] == pid), "")
                guess = {"person_id": pid, "name": name,
                         "similarity": round(best[pid], 4)}

        ids = [int(x) for x in folder_ids.split(",") if x.strip().isdigit()] or None
        meta = {}
        if hits:
            fids = list({h["file_id"] for h in hits})
            ph = ",".join("?" * len(fids))
            meta = {row["id"]: row for row in store._query(
                f"SELECT * FROM files WHERE id IN ({ph})", fids)}

        results = []
        for h in hits:
            row = meta.get(h["file_id"])
            if row is None:
                continue
            if ids and row["folder_id"] not in ids:
                continue
            if keep is not None and int(row["folder_id"] or 0) not in keep:
                continue                                     # 私有文件夹的脸照不外泄 ✓
            results.append({
                "id": int(row["id"]), "folder_id": row["folder_id"],
                "path": row["path"], "name": row["name"], "dir": row["dir"],
                "ext": row["ext"], "kind": row["kind"], "size": row["size"],
                "mtime": row["mtime"], "status": row["status"],
                "note": row["note"], "n_chunks": row["n_chunks"],
                "score": round(h["sim"] * 100, 3),
                "channels": {"face": {"rank": 0, "score": round(h["sim"], 4)}},
                "snippet": "",
            })
        return {"count": len(results), "threshold": thr,
                "guess": guess, "results": results[:top_k]}

    # ---------------------------------------------------------- 打开文件
    def _indexed_file(file_id: int) -> tuple[dict, Path]:
        row = store.get_file(file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="索引里没有这个文件，请先建立索引")
        # ★ 服务器上库里存的是 Windows 路径 → 换成本机能打开的路径 ✓
        #   项目主 2026-10-20 实测：不换的话预览直接 403「只能操作已索引文件夹里的文件」
        path = _to_this_host(row["path"])
        if not _is_inside_indexed(path):
            raise HTTPException(status_code=403, detail="只能操作已索引文件夹里的文件")
        return row, path

    @router.post("/api/open")
    async def open_path(request: Request, payload: dict = Body(...)):
        raw = (payload.get("path") or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="缺少 path")
        _need_path_read(request, raw)                          # 别人的私有文件别想打开 ✓
        target = _to_this_host(raw)                            # ★ 服务器上换成 /mnt 路径 ✓
        if not _is_inside_indexed(target):
            raise HTTPException(status_code=403, detail="只能打开已索引文件夹里的文件")
        message = _open_path(target, bool(payload.get("reveal")))
        return {"ok": True, "message": message}

    # ---------------------------------------------------------- 预览
    @router.get("/api/preview/{file_id}")
    async def preview_raw(file_id: int, download: int = 0):
        """把文件本体发给浏览器，用于图片/音频/视频/PDF 预览。"""
        row, path = _indexed_file(file_id)
        if not os.path.isfile(str(path)):
            raise HTTPException(status_code=404,
                                detail="文件当前读不到（网络共享可能未连接）")
        media = MEDIA_TYPES.get(str(row["ext"]).lower()) \
            or mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
        headers = {"Cache-Control": "private, max-age=120"}
        if download:
            headers["Content-Disposition"] = attachment_header(row["name"])
        return FileResponse(path, media_type=media, headers=headers)

    @router.get("/api/preview/{file_id}/image")
    async def preview_image(file_id: int, w: int = 1600):
        """图片预览专用：服务端先解码，至少保证「能显示就是真能显示」。

        直接发原文件的话，遇到内容其实不是合法图片的文件（例如还没同步完的
        占位文件），浏览器会静默渲染失败、只留一个空白框；HEIC/TIFF 更是
        完全不支持。所以这里统一解码后再输出 JPEG。
        """
        row, path = _indexed_file(file_id)
        if not os.path.isfile(str(path)):
            raise HTTPException(status_code=404, detail="文件当前读不到（网络共享可能未连接）")
        ext = str(row["ext"]).lower()
        limit = max(64, min(int(w), 4000))

        try:
            image = decode_image(path)
        except InputError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=415, detail=f"这个文件显示不了：{exc}") from exc

        longest = max(image.size)
        if longest > limit:
            ratio = limit / longest
            image = image.resize((max(1, int(image.width * ratio)),
                                  max(1, int(image.height * ratio))))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=86)
        return Response(content=buffer.getvalue(), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=300"})

    @router.get("/api/raw")
    async def raw_file(path: str, w: int = 0):
        """按**路径**取文件内容。

        为什么需要：剪藏文章的插图文件名是哈希（c09f8034..._MD5.jpg），
        无法用文字搜到，只能靠 .md 正文里的相对路径引用。
        预览 .md 时前端把 `![](../_resources/x.jpg)` 解析成绝对路径，
        再走这个接口把图取出来显示。

        安全：只允许读取「已索引文件夹」范围内的文件。
        """
        target = Path(path)
        try:
            real = safe_resolve(target)
        except OSError:
            raise HTTPException(status_code=400, detail="路径无效") from None
        roots = _allowed_roots()
        low = str(real).lower()
        if not any(low.startswith(str(r).lower()) for r in roots):
            raise HTTPException(status_code=403, detail="这个路径不在已索引的文件夹里")
        if not real.is_file():
            raise HTTPException(status_code=404, detail="文件不存在或读不到")

        ext = real.suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
                   ".heic", ".heif", ".avif", ".tif", ".tiff"):
            try:
                # 注意：decode_image 要 Path，不是 str（它会调 .stat()）
                image = decode_image(real)
            except Exception as exc:                      # noqa: BLE001
                raise HTTPException(status_code=415,
                                    detail=f"这个图片显示不了：{exc}") from exc
            limit = max(64, min(int(w), 4000)) if w else 1600
            longest = max(image.size)
            if longest > limit:
                ratio = limit / longest
                image = image.resize((max(1, int(image.width * ratio)),
                                      max(1, int(image.height * ratio))))
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=86)
            return Response(content=buf.getvalue(), media_type="image/jpeg")
        return FileResponse(str(real))

    # ---------------------------------------------------------- 界面偏好
    # ---------------- ☁ Dify 共享（2026-10-22 项目主："做共享"）----------------
    @router.get("/api/dify/shared")
    def dify_shared(request: Request):
        """别人共享给我的 Dify 库（**不带密钥**，只用来在界面上列出来）。"""
        _need_login(request)
        from . import difyshare

        u = _who(request) or {}
        items = difyshare.visible_to(store, int(u.get("id") or 0), str(u.get("name") or ""))
        return {"ok": True, "items": [
            {"name": x.get("name") or "", "owner": x.get("_owner") or "",
             "dataset_id": x.get("dataset_id") or ""} for x in items]}

    @router.post("/api/dify/validate")
    def dify_validate(request: Request, payload: dict = Body(default={})):
        """校验「共享给」里填的用户名：存在的返回 valid，查无此人的返回 unknown。"""
        _need_login(request)
        from . import difyshare

        raw = payload.get("names") or []
        if isinstance(raw, str):
            raw = raw.replace("，", ",").split(",")
        names = [str(x).strip() for x in raw if str(x).strip()]
        ok, bad = difyshare.resolve_names(store, names)
        return {"ok": True, "valid": ok, "unknown": bad}

    # ---------------------------------------------------------- 默认排除目录（设置项）
    @router.get("/api/settings/sync")
    async def get_sync(request: Request):
        """读「服务器地址」。支持多个地址，返回每个的连通性（自适应用）。"""
        _need_admin_or_local(request)
        from . import syncpush

        raw = (cfg._get("sync", "server", "") or "").strip()
        cur = ""
        cands: list[dict] = []
        try:
            cur = syncpush.pick_server(cfg)
            cands = syncpush.candidates(cfg)
        except Exception:                                        # noqa: BLE001
            pass
        return {"ok": True, "server": raw, "current": cur,
                "candidates": cands,
                "user": (cfg._get("sync", "user", "") or "").strip(),
                # ★ 2026-10-22 项目主问"账号、密码怎么填？只有一个框啊" ——
                #   凭据一直存在 config.ini 的 [sync] password / token 里，
                #   但界面没有输入框，只能手改文件 ✗
                #   这里**只回报有没有**，绝不把密码/令牌回给浏览器 ✓
                "has_password": bool((cfg._get("sync", "password", "") or "").strip()),
                "has_token": bool((cfg._get("sync", "token", "") or "").strip()),
                # 本机这一侧的身份（项目主 2026-10-22："能确认本机版本（管理员/用户）吗"）
                "is_builder": bool(_can_index_here()),
                "view_mode": _view_mode(str((_who(request) or {}).get("role") or "")),
                "me": {"name": str((_who(request) or {}).get("name") or ""),
                       "role": str((_who(request) or {}).get("role") or "")},
                "path": str(cfg.path)}

    @router.post("/api/settings/sync/verify")
    async def verify_sync(request: Request, payload: dict = Body(...)):
        """**用账号密码真的登录一次服务器**，确认凭据对、并拿到你在服务器上的身份。

        2026-10-22 项目主提的两件事都在这里落地：
          ① "不是应该需要强制填账号密码才能连接服务器吗" → 没账号/密码直接 400 ✓
          ② "也能通过连接服务器后确认本机版本（管理员/用户）吗" →
             登录成功后回 `user.role`（服务器上的管理员/普通用户）✓
        `save=true` 时顺手把这组凭据连同换到的 token 写进 config.ini（推库直接用 ✓）。
        """
        _need_admin_or_local(request)
        from . import syncpush

        server = str(payload.get("server") or cfg._get("sync", "server", "") or "").strip()
        user = str(payload.get("user") or cfg._get("sync", "user", "") or "").strip()
        pwd = str(payload.get("password") or "")
        if not pwd:
            # 没现填就用已保存的（改地址不用重输密码）
            pwd = str(cfg._get("sync", "password", "") or "")
        if not server:
            raise HTTPException(status_code=400, detail="先填服务器地址")
        if not user:
            raise HTTPException(status_code=400, detail="必须填账号（服务器上的用户名）")
        if not pwd:
            raise HTTPException(status_code=400, detail="必须填密码（和服务器上那个账号对应）")
        # 多地址时先探测，挑第一个通的
        base = server
        try:
            syncpush._PICK_CACHE.update({"at": 0.0, "base": "", "cands": []})  # noqa: SLF001
            cands = syncpush.candidates(cfg) if not payload.get("server") else []
            if cands:
                for c in cands:
                    if c.get("ok"):
                        base = c["server"]
                        break
        except Exception:                                        # noqa: BLE001
            base = server.split(",")[0].strip() if "," in server else server
        if "," in base:
            base = base.split(",")[0].strip()

        # ⚠ 防呆：服务器地址填成本机自己 → 这次"验证"会自己调自己。
        #   本机服务是单线程的，等于把自己锁死，60 秒后只会报一句 timed out ✗
        try:
            from urllib.parse import urlparse as _up

            _u = _up(base)
            _host = (_u.hostname or "").lower()
            _port = _u.port or (443 if _u.scheme == "https" else 80)
            _myport = int(str(cfg._get("server", "port", "8199")))
            if _host in ("127.0.0.1", "localhost", "::1") and int(_port) == _myport:
                raise HTTPException(
                    status_code=400,
                    detail="这个地址就是本机自己：验证会自己调自己，只会超时。"
                           "请填服务器那台机器的地址（例如 http://192.168.1.10:8199）")
        except HTTPException:
            raise
        except Exception:                                        # noqa: BLE001
            pass

        try:
            tok = syncpush.login(base, user, pwd)
        except HTTPException:
            raise
        except Exception as exc:                                 # noqa: BLE001
            msg = str(exc)
            low = msg.lower()
            if "401" in msg or "unauthor" in low:
                msg = "账号或密码不对（服务器拒绝了这次登录）"
            elif "timed out" in low or "timeout" in low:
                msg = "连接超时：地址 / 端口对不对？服务器开着吗？"
            elif "refused" in low:
                msg = "连不上：那个地址的端口没有服务在听"
            raise HTTPException(status_code=400,
                                detail=f"登录服务器失败：{msg}") from exc

        # 登录成功 → 再问一次"我是谁"，确认服务器认的角色
        me = {}
        try:
            import urllib.request

            req = urllib.request.Request(
                base.rstrip("/") + "/api/auth/me",
                headers={"Cookie": "eg2_sid=" + tok})
            with urllib.request.urlopen(req, timeout=10) as resp:
                me = json.loads(resp.read().decode("utf-8", "replace")).get("user") or {}
        except Exception as exc:                                 # noqa: BLE001
            log.warning("取服务器身份失败（登录已经成功）：%s", exc)

        saved = []
        if payload.get("save"):
            try:
                values = {"sync.server": server, "sync.user": user,
                          "sync.password": pwd, "sync.token": tok,
                          # 记下"验证通过的我是谁"，上传卡据此显示身份（只认这个）✓
                          "sync.verified_name": str((me or {}).get("name") or user),
                          "sync.verified_role": str((me or {}).get("role") or ""),
                          "sync.verified_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                saved = cfg.update(values)
                cfg.reload()
            except Exception as exc:                             # noqa: BLE001
                log.warning("保存同步凭据失败：%s", exc)
        log.info("服务器凭据验证通过：%s@%s 角色=%s", user, base, me.get("role"))
        return {"ok": True, "server": base, "user": me or {"name": user},
                "saved": saved, "local_view_mode": _view_mode(
                    str((_who(request) or {}).get("role") or ""))}

    @router.put("/api/settings/sync")
    async def set_sync(request: Request, payload: dict = Body(...)):
        """改服务器地址 / 同步凭据：写 config.ini → 立刻生效 ✓，并试连一下报结果。

        凭据三选一（`app/syncpush.py` 的优先级）：
          ① `[sync] token`  手填的会话令牌（最省事，**优先**用）
          ② `[sync] user` + `[sync] password` 用账号密码换一个会话
          ③ 都不填 → 推库会直接报"还没设服务器凭据"
        `password` / `token` **留空 = 不改**（免得每次点保存都把已有凭据清掉 ✗）
        """
        _need_admin_or_local(request)
        server = str(payload.get("server") or "").strip()
        user = str(payload.get("user") or "").strip()
        if not server:
            raise HTTPException(status_code=400, detail="服务器地址不能为空")
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        values = {"sync.server": server, "sync.user": user}
        # 只有真的填了才写；"留空=不改" 这样点两次保存也不会把凭据搞丢 ✓
        new_pwd = str(payload.get("password") or "")
        new_tok = str(payload.get("token") or "")
        if new_pwd.strip():
            values["sync.password"] = new_pwd.strip()
        if new_tok.strip():
            values["sync.token"] = new_tok.strip()
        try:
            changed = cfg.update(values)
            cfg.reload()                      # ★ 立即生效，不用重启 ✓
        except Exception as exc:                                  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"写入 config.ini 失败：{exc}") from exc
        # 试连一下（失败只提示，不算保存失败——服务器可能暂时没开）
        # 探测每个候选地址（支持逗号分隔的多个）→ 立刻告诉用户选了哪个 ✓
        from . import syncpush

        cands: list[dict] = []
        probe = {"ok": False, "detail": "", "chosen": ""}
        try:
            syncpush._PICK_CACHE.update({"at": 0.0, "base": "", "cands": []})  # noqa: SLF001
            cands = syncpush.candidates(cfg)
            chosen = syncpush.pick_server(cfg, force=True)
            hit = next((x for x in cands if x["server"] == chosen), None)
            probe = {"ok": bool(hit and hit["ok"]), "chosen": chosen,
                     "detail": ("连通 ✓" if (hit and hit["ok"])
                                else "都不通，已存下但先连不上")}
        except Exception as exc:                                  # noqa: BLE001
            probe = {"ok": False, "chosen": "", "detail": str(exc)[:120]}
        log.info("服务器地址已更新为 %s（连通=%s）", server, probe["ok"])
        return {"ok": True, "server": server, "user": user,
                "changed": changed, "probe": probe, "candidates": cands}

    @router.get("/api/settings/ignore-dirs")
    async def get_ignore_dirs(request: Request):
        """默认排除目录：索引扫描和"子目录列表"都按它过滤。

        项目主 2026-10-18：要能在设置里自己改 ✓（以前只能改 config.ini ✗）
        """
        _need_admin_or_local(request)
        from . import folderconf as FC
        # ⚠ 用原文（`ignore_dirs` 属性是小写的，界面上显示 "@recently-snapshot" 不好看 ✗）
        raw = cfg._get("search", "ignore_dirs", "") or ""
        keep: list[str] = []
        for item in raw.replace("\n", ",").split(","):
            name = item.strip()
            if name and name not in keep:
                keep.append(name)
        return {
            "ok": True,
            "dirs": keep or sorted(cfg.ignore_dirs, key=str.lower),
            "presets": list(FC.DEFAULT_IGNORE_DIRS),
            "path": str(cfg.path),
        }

    @router.put("/api/settings/ignore-dirs")
    async def set_ignore_dirs(request: Request, payload: dict = Body(...)):
        """保存默认排除目录：写 config.ini（保留注释）→ 立刻 reload ✓ 不用重启。"""
        _need_admin_or_local(request)
        dirs = payload.get("dirs")
        if not isinstance(dirs, list):
            raise HTTPException(status_code=400, detail="dirs 需要是数组")
        clean: list[str] = []
        for item in dirs[:800]:
            name = str(item).strip().strip("/\\").strip()
            if not name or name in clean:
                continue
            clean.append(name)
        if not clean:
            raise HTTPException(status_code=400, detail="至少要留一条，否则会索引全部目录")
        try:
            changed = cfg.update({"search.ignore_dirs": ",".join(clean)})
            cfg.reload()                      # ★ 立即生效，不用重启进程 ✓
        except Exception as exc:                                  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"写入 config.ini 失败：{exc}") from exc
        log.info("默认排除目录已更新：%d 项（%s）", len(clean), changed)
        return {"ok": True, "dirs": clean, "count": len(clean), "changed": changed}

    # ---------------- 文件夹上传（本机 → 服务器）----------------
    # 项目主 2026-10-20："每人各自管理各自文件、上传同步进服务器"
    # 通道：本机打包（JSONL.gz）→ POST 到这里 → 按 path **并集**合并进主库，
    #      归属记成上传者本人 ✓ 只增/更新，从不删别人的东西 ✓
    _UPLOAD_MAX = 2 * 1024 * 1024 * 1024        # 单包上限 2 GB

    @router.post("/api/sync/folder-upload")
    async def folder_upload(request: Request, dry_run: bool = Query(False)):
        """接收一个文件夹数据包并合并进主库 ✓

        · **登录即可**（每人上传自己的，不要求管理员）—— 这正是"各人管各人的"
        · `dry_run=true` 时只统计不写库（"先看看会变成什么样"）
        · 合并只按 path 增/补，不会删任何人的东西 ✓
        """
        u = _need_login(request)
        uid = int((u or {}).get("id") or 0)
        from . import foldertransfer as _ft
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="包是空的")
        if len(body) > _UPLOAD_MAX:
            raise HTTPException(status_code=413,
                                detail=f"包太大了（{len(body)/1e9:.1f} GB，上限 2 GB）")
        import tempfile
        tmp = (Path(tempfile.gettempdir())
               / f"eg2-upload-{int(time.time() * 1000)}.jsonl.gz")
        tmp.write_bytes(body)
        try:
            # ★ 以**本机（服务器）的维度**为准校验，不信包里标的（见 import_pack 注释）
            stat = _ft.import_pack(store, tmp, owner_id=uid, dry_run=dry_run,
                                   expected_dim=int(cfg.index_dim),
                                   log=lambda m: log.info("上传导入：%s", m))
        except Exception as exc:                                 # noqa: BLE001
            log.warning("文件夹上传失败：%s", exc)
            raise HTTPException(status_code=500,
                                detail=f"导入失败：{exc}") from exc
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        log.info("文件夹上传：来自用户 %s（%s），dry_run=%s，%s",
                 uid, (u or {}).get("name"), dry_run, stat)
        # ★ 2026-10-21 项目主："用户文件夹的扫描维度需要与服务器上分类对齐才能合并搜索问答"
        #   所以把**本机的维度**和**服务器的维度**都带回去，前端才能醒目提示 ✓
        #   stat["dim"] 是包里标的（= 上传方的维度），server_dim 是这边（服务器）的
        # dim = 包里**实际观测到的**维度；server_dim = 这边（服务器）的
        # dim_ok 用两者的真实值比较，不再用 None 兜底成相等 ✗（那会把异构数据放进来）
        # 维度不同**不是错误**：系统按 dim 分组检索，混合维度本来就能一起搜 ✓
        # 只把它当建议统一的提示（统一了更省空间、检索更快）
        _seen = stat.get("seen_dim") or stat.get("dim")
        _dim_ok = (_seen is None) or (int(_seen) == int(cfg.index_dim))
        # ⚠ **stat 必须放前面：它里面也有 "dim"（是导入基准=服务器维度），
        #   放后面会把这里算好的"包里实际维度"覆盖掉 ✗（实测返回过 256，其实数据是 768）
        return {**stat, "ok": True, "dry_run": bool(dry_run), "owner_id": uid,
                "bytes": len(body), "server_dim": int(cfg.index_dim),
                "dim": _seen, "dim_ok": _dim_ok}

    @router.get("/api/sync/export")
    def sync_export(request: Request, folder_ids: str = Query(""),
                    dim: int = Query(0)):
        """把这台机器上的指定文件夹打成包（"本机 → 服务器"的第一步）✓

        `folder_ids` 用逗号分隔；不传 = 所有我能写的文件夹。
        普通用户只会拿到自己名下的 ✓
        """
        _need_login(request)
        from . import foldertransfer as _ft
        raw_ids = str(folder_ids or "").strip()
        want: list[int] = []
        for part in raw_ids.split(","):
            part = part.strip()
            if part.isdigit():
                want.append(int(part))
        # ⚠ 2026-10-20 踩到：folder_ids=-1 这种**非法值**解析不出 id →
        #   落进了"不传就导全部"的分支，一口气打了 121 MB 的包 ✗
        #   传了但一个都解析不出来，必须报错，绝不偷偷导出整库 ✓
        if raw_ids and not want:
            raise HTTPException(status_code=400,
                                detail=f"folder_ids 里没有有效的文件夹编号：{raw_ids}")
        if not want:
            rows = store._query("SELECT id FROM folders ORDER BY id")   # noqa: SLF001
            want = [int(r["id"]) for r in rows]
            w = _writable(request)
            if w is not None:
                want = [i for i in want if i in w]
        import tempfile
        out = (Path(tempfile.gettempdir())
               / f"eg2-export-{int(time.time() * 1000)}.jsonl.gz")
        stat = _ft.export_folders(store, want, out, note="界面导出",
                                  dim=(dim or None))
        return JSONResponse({"ok": True, **stat, "file": str(out)})

    @router.get("/api/sync/download")
    def sync_download(request: Request, name: str = Query(...)):
        """下载导出好的包（只允许临时目录里的 eg2-export-*）✓"""
        _need_login(request)
        p = Path(tempfile.gettempdir()) / Path(name).name
        if not p.name.startswith("eg2-export-") or not p.exists():
            raise HTTPException(status_code=404, detail="没有这个导出包")
        return FileResponse(str(p), media_type="application/gzip",
                            filename=p.name)

    # ---------------------------------------------------------- 界面偏好
    # ---------------- 本机 → 服务器 推送（界面按钮用）----------------
    # 项目主 2026-10-20："每人各自管理各自文件、上传同步进服务器"
    # 和 /api/sync/folder-upload 的区别：**那个是"接收端"（服务器用）**，
    # 这三个是"发送端"（本机用）：打包 → 登录服务器 → 上传，带进度 ✓
    # ⚠ 只有本机（建库机）才该出现这个功能；服务器上推给自己没意义 ✓

    @router.get("/api/sync/push/status")
    def sync_push_status(request: Request):
        _need_login(request)
        from . import syncpush as _sp
        return {"ok": True, "can_push": _can_index_here(),
                "account": _sp.account(cfg),
                # ★ 2026-10-22 项目主："上传身份不能在这张卡里随便填" →
                #   只认「服务器连接」里**验证通过**的那一组（verify 时写进 config）
                "verified": {
                    "name": (cfg._get("sync", "verified_name", "") or "").strip(),
                    "role": (cfg._get("sync", "verified_role", "") or "").strip(),
                    "at": (cfg._get("sync", "verified_at", "") or "").strip(),
                },
                **_sp.status()}

    @router.put("/api/sync/account")
    def sync_account(request: Request, payload: dict = Body(...)):
        """保存服务器的地址 / 账号 / 密码（本机版"自定义服务器地址"就靠它）✓"""
        _need_admin_or_local(request)
        from .config import patch_ini
        vals = {}
        if "server" in payload:
            s = str(payload.get("server") or "").strip().rstrip("/")
            if s and not (s.startswith("http://") or s.startswith("https://")):
                raise HTTPException(status_code=400,
                                    detail="服务器地址要以 http:// 或 https:// 开头")
            vals["server"] = s
        if "user" in payload:
            vals["user"] = str(payload.get("user") or "").strip()
        if "password" in payload:
            vals["password"] = str(payload.get("password") or "")
        if "token" in payload:
            vals["token"] = str(payload.get("token") or "").strip()
        ini = Path(cfg.path) if getattr(cfg, "path", None) else ROOT / "config.ini"
        patch_ini(ini, "sync", vals)
        try:
            cfg.reload()
        except Exception as exc:                                 # noqa: BLE001
            log.warning("重载配置失败：%s", exc)
        from . import syncpush as _sp
        return {"ok": True, **_sp.account(cfg)}

    @router.post("/api/sync/push")
    def sync_push(request: Request, payload: dict = Body(default={})):
        """把选中的文件夹推给服务器（`dry_run=true` 只是预演，服务器不写库）✓"""
        _need_admin_or_local(request)
        if not _can_index_here():
            raise HTTPException(
                status_code=400,
                detail="这台是服务器：数据本来就是从本机推上来的，不用再推 ✓")
        from . import syncpush as _sp
        # ★ 2026-10-22 项目主："上传身份不能在这张卡里随便填" ——
        #   上传只认「服务器连接」里验证通过的那组凭据；
        #   一个凭据都没有（账号密码 / 令牌）就直接挡掉，免得传到错的人名下 ✓
        _acct = _sp.account(cfg)
        if not _acct.get("has_token") and not (_acct.get("user")
                                               and _acct.get("has_password")):
            raise HTTPException(
                status_code=400,
                detail="还没验证过服务器身份：去「设置 → 服务器与共享 → 服务器连接」，"
                       "填账号和密码点「保存并验证」，通过后才能上传 ✓")
        ids = [int(x) for x in (payload.get("folder_ids") or []) if str(x).isdigit()]
        if not ids:
            raise HTTPException(status_code=400, detail="先选要上传的文件夹")
        dry = bool(payload.get("dry_run"))
        if not _sp.start_async(cfg, store, ids, dry_run=dry):
            return {"ok": False, "message": "上一次还在跑，等它结束"}
        return {"ok": True, "folders": len(ids), "dry_run": dry,
                "message": ("已开始预演" if dry else "已开始上传")}

    # ---------------- 我的数据 / 撤下（成员视角）----------------
    # 项目主 2026-10-20："能撤下自己的，但不能删别人的"
    # 撤下 = 从**服务器**移除这份数据（本机不受影响，本机才是源 ✓）

    def _my_uid(request) -> tuple[int, str]:                      # noqa: ANN001
        u = _who(request) or {}
        return int(u.get("id") or 0), str(u.get("role") or "")

    @router.get("/api/my/data")
    def my_data(request: Request):
        """我上传到这里的文件夹（服务器上讲 owner_id，本机就是全部）✓"""
        _need_login(request)
        uid, role = _my_uid(request)
        local = _can_index_here()
        if local:
            # 本机：这台机器上的一切都是"我的"，不用按归属过滤 ✓
            where, args = "1=1", ()
        elif role == "admin" or _local_open(cfg.local_strict):
            # 服务器管理员：能看到所有人的（但**撤下**仍然只限自己的，见下）
            where, args = "1=1", ()
        else:
            where, args = "COALESCE(f.owner_id,0)=?", (uid,)
        rows = store._query(                                    # noqa: SLF001
            "SELECT f.id, f.path, f.visibility, f.privacy, "
            "       COALESCE(f.owner_id,0) AS owner_id, f.category, "
            "  (SELECT COUNT(*) FROM files fi WHERE fi.folder_id=f.id) AS n_files, "
            "  (SELECT COUNT(*) FROM chunks ch JOIN files fi ON fi.id=ch.file_id "
            "     WHERE fi.folder_id=f.id) AS n_chunks, "
            "  (SELECT COALESCE(SUM(fi.size),0) FROM files fi "
            "     WHERE fi.folder_id=f.id) AS bytes "
            "FROM folders f WHERE " + where + " ORDER BY f.id", args)
        items = []
        for r in rows:
            items.append({
                "id": int(r["id"]), "path": r["path"],
                "visibility": r["visibility"] or "private",
                "privacy": bool(int(r["privacy"] or 0)),
                "owner_id": int(r["owner_id"] or 0),
                "category": r["category"] or "",
                "n_files": int(r["n_files"] or 0),
                "n_chunks": int(r["n_chunks"] or 0),
                "mb": round(int(r["bytes"] or 0) / 1e6, 1),
                # 只有"归属是自己"（本机则全部）才能撤下 ✓
                "can_withdraw": bool(local or int(r["owner_id"] or 0) == uid),
            })
        mine = sum(1 for x in items if x["can_withdraw"])
        return {"ok": True, "local": local, "role": role, "user_id": uid,
                "items": items, "count": len(items), "mine": mine,
                "total_mb": round(sum(x["mb"] for x in items), 1)}

    @router.post("/api/my/withdraw")
    def my_withdraw(request: Request, payload: dict = Body(...)):
        """把自己传上去的文件夹从**这里**撤下 ✓

        · 只允许撤**归属是自己**的（服务器上）；本机随便撤（都是你的）
        · 撤下会删掉这边这个文件夹的文件/片段/向量；**本机的数据一点不动** ✓
        · 要显式 confirm，避免误点
        """
        _need_login(request)
        uid, role = _my_uid(request)
        local = _can_index_here()
        fid = int(payload.get("folder_id") or 0)
        if not fid:
            raise HTTPException(status_code=400, detail="要指定 folder_id")
        if not payload.get("confirm"):
            raise HTTPException(status_code=400, detail="要带 confirm=true 才执行")
        rows = store._query(                                    # noqa: SLF001
            "SELECT id, path, COALESCE(owner_id,0) AS owner_id FROM folders WHERE id=?",
            (fid,))
        if not rows:
            raise HTTPException(status_code=404, detail="没有这个文件夹")
        owner = int(rows[0]["owner_id"] or 0)
        fpath = str(rows[0]["path"] or "")
        # ★ 核心规则："能撤下自己的，但不能删别人的"
        if not local and owner != uid:
            raise HTTPException(
                status_code=403,
                detail=f"这个文件夹不是你上传的（归属用户 {owner or '无主'}），撤不了。"
                       "只有管理员能在服务器上清理无主数据 ✓")
        store.remove_folder(fid)
        log.info("撤下文件夹 #%s「%s」（操作人 %s，归属 %s，本机=%s）",
                 fid, fpath, uid, owner, local)
        return {"ok": True, "removed": fid, "path": fpath, "owner_id": owner,
                "message": "已从这台服务器撤下（你本机上的数据不受影响 ✓）"}

    # ---------------- 共享给指定人（2026-10-21 项目主要求）----------------
    # 三种状态（语义由 folders.visibility + folder_acl 共同决定）：
    #   · private  + 没有 acl  → 只有自己和管理员
    #   · shared               → 全站登录用户都能看
    #   · private  + acl 有人  → **只共享给这几个人** ← 项目主要的就是这个 ✓
    # 读取侧不用改：`multiuser.visible_folder_ids()` 早就按这套规则算了。

    @router.get("/api/folders/{folder_id}/shares")
    def folder_shares_get(request: Request, folder_id: int):
        """看这个文件夹现在共享给谁（只有主人/管理员能看）✓"""
        _need_folder_write(request, folder_id)
        rows = store._query(                                    # noqa: SLF001
            "SELECT id, path, visibility, COALESCE(owner_id,0) AS owner_id "
            "FROM folders WHERE id=?", (folder_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="没有这个文件夹")
        folder = rows[0]
        granted = [int(r["user_id"]) for r in store._query(     # noqa: SLF001
            "SELECT user_id FROM folder_acl WHERE folder_id=? AND can_read=1",
            (folder_id,))]
        users = [{"id": int(u["id"]), "name": u["name"], "role": u["role"],
                  "state": u["state"]}
                 for u in store._query(                          # noqa: SLF001
                     "SELECT id, name, role, state FROM users "
                     "WHERE state='active' ORDER BY id")]
        return {"ok": True, "folder_id": folder_id,
                "path": folder["path"],
                "visibility": folder["visibility"] or "private",
                "owner_id": int(folder["owner_id"] or 0),
                "granted": granted, "users": users}

    @router.put("/api/folders/{folder_id}/shares")
    def folder_shares_set(request: Request, folder_id: int,
                          payload: dict = Body(...)):
        """设置"共享给谁"✓

        传 `visibility`（'private' / 'shared'）和 `user_ids`（指定能看的人）。
        语义：
          · visibility='shared' → 全站可看（`user_ids` 仍会记下，但不影响）
          · visibility='private' + user_ids=[...] → **只给这几个人**（项目主要的）
          · visibility='private' + user_ids=[]   → 只有自己
        """
        _need_folder_write(request, folder_id)
        rows = store._query("SELECT id, path FROM folders WHERE id=?", (folder_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="没有这个文件夹")
        me = _who(request) or {}
        my_uid = int(me.get("id") or 0)
        vals = {}
        if "visibility" in payload:
            vis = str(payload.get("visibility") or "private").strip()
            if vis not in ("private", "shared"):
                raise HTTPException(status_code=400,
                                    detail="visibility 只能是 private 或 shared")
            vals["visibility"] = vis
        ids: list[int] | None = None
        if "user_ids" in payload:
            raw = payload.get("user_ids") or []
            ids = []
            for x in raw:
                try:
                    i = int(x)
                except (TypeError, ValueError):
                    continue
                if i and i != my_uid:          # 不把自己塞进"共享给"
                    ids.append(i)
            ids = sorted(set(ids))
        with store._conn:                                        # noqa: SLF001
            if vals:
                store._conn.execute(                            # noqa: SLF001
                    "UPDATE folders SET visibility=? WHERE id=?",
                    (vals["visibility"], folder_id))
            if ids is not None:
                store._conn.execute(                            # noqa: SLF001
                    "DELETE FROM folder_acl WHERE folder_id=?", (folder_id,))
                for uid in ids:
                    store._conn.execute(                        # noqa: SLF001
                        "INSERT OR REPLACE INTO folder_acl(folder_id, user_id, can_read) "
                        "VALUES(?,?,1)", (folder_id, uid))
        store._invalidate()                                      # noqa: SLF001
        log.info("共享设置：#%s「%s」visibility=%s，给 %s 个人看",
                 folder_id, rows[0]["path"], vals.get("visibility", "不变"),
                 len(ids) if ids is not None else "不变")
        return {"ok": True, "folder_id": folder_id,
                "visibility": vals.get("visibility"),
                "user_ids": ids}

    @router.get("/api/shared-with-me")
    def shared_with_me(request: Request):
        """别人共享给我的文件夹（成员视角）✓

        两类都算：
          · 别人把文件夹设成 `shared`（全站共享）
          · 别人在 `folder_acl` 里点名给我读
        自己拥有的不算（那在「我的数据」里）。
        """
        u = _need_login(request)
        me = u or {}
        uid = int(me.get("id") or 0)
        rows = store._query(                                    # noqa: SLF001
            "SELECT f.id, f.path, f.visibility, COALESCE(f.owner_id,0) AS owner_id, "
            "       u.name AS owner_name, "
            "  (SELECT COUNT(*) FROM files fi WHERE fi.folder_id=f.id) AS n_files, "
            "  (SELECT COUNT(*) FROM chunks ch JOIN files fi ON fi.id=ch.file_id "
            "     WHERE fi.folder_id=f.id) AS n_chunks "
            "FROM folders f LEFT JOIN users u ON u.id=f.owner_id "
            "WHERE COALESCE(f.owner_id,0) <> ? "
            "  AND ( f.visibility='shared' "
            "        OR EXISTS (SELECT 1 FROM folder_acl a "
            "                    WHERE a.folder_id=f.id AND a.user_id=? AND a.can_read=1) ) "
            "ORDER BY f.id", (uid, uid))
        items = []
        for r in rows:
            items.append({
                "id": int(r["id"]), "path": r["path"],
                "owner_id": int(r["owner_id"] or 0),
                "owner": r["owner_name"] or ("无主" if not r["owner_id"] else "?"),
                "visibility": r["visibility"] or "private",
                "n_files": int(r["n_files"] or 0),
                "n_chunks": int(r["n_chunks"] or 0),
            })
        return {"ok": True, "items": items, "count": len(items),
                "user_id": uid}

    @router.get("/api/admin/user-dbs")
    def user_dbs_status(request: Request):
        """多人版地基：看"哪个用户已经独立成库、哪个还在主库" ✓

        现在所有用户都还在主库上（`split=false`），这是**预期**的：
        地基只负责"有用户库就用它的，没有就用主库"，行为不变。
        """
        _need_admin(request)
        from . import userdb as _udb
        return {"ok": True, **_udb.status(cfg)}

    # ---------------------------------------------------------- 界面偏好
    # ★ 偏好分两层（项目主 2026-10-20 定的）：
    #   · **系统设置**（模型 API / 打标模型 / 知识库引擎 / 排除目录 / 备份快照）：
    #     管理员改 → **同时写全局默认**（所有人的缺省），普通用户改 → 只写自己的
    #     （所以"模型各自用各自的"成立，而管理员设的是大家没设过时用的那份）
    #   · **个人爱好**（折叠状态 / 上次标签页 / 视图模式 / PDF 缩放）：
    #     永远只写自己，管理员改了也不该影响别人 ✓
    #   没匹配上的键一律按"个人"处理（安全：免得误改全局）
    SYSTEM_PREF_PREFIXES = (
        "eg2-rm",      # 外部问答模型：url / key / model / hdr / apilist / presets…
        "eg2-vl",      # 打标模型：model / modelname / remote
        "eg2-kb",      # 知识库检索引擎
        "eg2-ignore",  # 默认排除目录
        "eg2-backup",  # 备份设置
        "eg2-snap",    # 快照 / 异地副本设置
    )

    def _is_system_pref(name: str) -> bool:
        """这个偏好算"系统设置"吗（管理员改就该成为大家的默认）✓"""
        return str(name).startswith(SYSTEM_PREF_PREFIXES)

    def _pref_uid(request) -> int:                               # noqa: ANN001
        """当前该用哪个用户的偏好（0 = 全局/匿名）✓

        多人版（2026-10-20）：界面偏好按用户存。管理员登录后读到的是
        "全局默认 + 他自己的覆盖"，普通用户同理互不影响 ✓
        """
        try:
            u = _who(request)
        except Exception:                                        # noqa: BLE001
            u = None
        try:
            return int((u or {}).get("id") or 0)
        except (TypeError, ValueError):
            return 0

    @router.get("/api/ui-prefs")
    async def ui_prefs_get(request: Request):
        """界面偏好（卡片/分区的折叠状态、上次的标签页、视图模式…）。

        浏览器本来存在 localStorage 里，但 stop.bat 用 taskkill /f 关浏览器窗口，
        Chrome 来不及把 localStorage 落盘就被杀了 —— 重启后设置全丢、又要重设一遍。
        所以服务端也留一份（data/index/index.db 的 meta 表），页面打开时回灌。

        ★ 2026-10-20 多人版：**按登录用户各存一份** ✓
          以前是全局一份 → 两个人用同一个服务时，谁的模型密钥最后设的就算谁的 ✗
          现在：user_prefs 里按 user_id 存，读的时候"全局默认 + 自己的覆盖"叠起来
          （没登录 → 只读全局默认，和以前一样 ✓）
        """
        _me = _who(request) or {}
        _uid = _pref_uid(request)
        _adm = (_me.get("role") == "admin" or _uid == 0
                or _can_index_here())
        return {"ok": True,
                # ★ 普通用户不继承管理员的 API 凭据（eg2-rm*）—— 密钥不能给他看 ✓
                "prefs": store.prefs_all(_uid, inherit_sensitive=_adm),
                "scope": ("global" if _uid == 0 else ("admin" if _adm else "user"))}

    @router.post("/api/ui-prefs")
    async def ui_prefs_set(request: Request, payload: dict = Body(...)):
        """保存界面偏好。只收 eg2- 开头的键，别的键一律不写。

        多人版：写进**当前登录用户**名下；未登录时写全局（兼容老行为）✓
        """
        uid = _pref_uid(request)
        me = _who(request) or {}
        is_admin = (me.get("role") == "admin" or _can_index_here())
        values = payload.get("values")
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="values 需要是对象")
        saved: list[str] = []
        removed: list[str] = []
        global_touched: list[str] = []
        for key, value in list(values.items())[:200]:
            name = str(key)
            if not name.startswith("eg2-") or len(name) > 80:
                continue
            if value is None:
                store.prefs_delete(name, uid)
                removed.append(name)
            else:
                store.prefs_set(name, str(value)[:4000], uid)
                saved.append(name)
            # ★ 管理员改**系统设置** → 同时成为所有人的默认（user_id=0）✓
            #   个人爱好（折叠/标签页/视图）不走这条路，免得管理员的界面习惯
            #   被推给别人 ✗
            if is_admin and _is_system_pref(name):
                if value is None:
                    store.prefs_delete(name, 0)
                else:
                    store.prefs_set(name, str(value)[:4000], 0)
                global_touched.append(name)
        return {"ok": True, "saved": saved, "removed": removed,
                "scope": ("user" if uid else "global"),
                "global": global_touched}

    # ---------------------------------------------------------- PDF 按页看
    @router.get("/api/preview/{file_id}/pdf-pages")
    async def preview_pdf_pages(file_id: int):
        """PDF 按页给文字，并告诉前端能不能出图（看原版式）。

        不重新索引、不动数据库：预览的时候用 pypdf 现读，
        所以页码是准的，「这段在第几页」终于答得上来。
        """
        row, path = _indexed_file(file_id)
        if not os.path.isfile(str(path)):
            raise HTTPException(status_code=404, detail="文件当前读不到（网络共享可能未连接）")
        try:
            from pypdf import PdfReader
        except ImportError:
            raise HTTPException(status_code=503, detail="没装 pypdf，无法按页读取 PDF") from None
        try:
            reader = PdfReader(str(path))
            total = len(reader.pages)
            pages: list[dict[str, Any]] = []
            for index in range(min(total, _PDF_PAGE_MAX)):
                try:
                    text = reader.pages[index].extract_text() or ""
                except Exception:                              # noqa: BLE001
                    text = ""
                pages.append({"page": index + 1, "chars": len(text),
                              "text": text[:_PDF_PAGE_CHARS]})
        except Exception as exc:                               # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"读 PDF 失败：{exc}") from exc
        return {"ok": True, "name": row["name"], "total_pages": total, "pages": pages,
                "render": pdf_render_available(),
                "truncated": total > len(pages),
                "note": "" if pdf_render_available()
                        else "装 PyMuPDF 才能看原版式（表格样式）"}

    @router.get("/api/preview/{file_id}/pdf-page")
    async def preview_pdf_page(file_id: int, page: int = 1, w: int = 1400):
        """把 PDF 的某一页渲染成 PNG —— 这样表格、合并单元格、版式都看得见。"""
        row, path = _indexed_file(file_id)
        if not os.path.isfile(str(path)):
            raise HTTPException(status_code=404, detail="文件当前读不到（网络共享可能未连接）")
        try:
            import fitz                                            # PyMuPDF
        except ImportError:
            raise HTTPException(
                status_code=503,
                detail="要「看原版式」得先装 PyMuPDF："
                       "venv\\Scripts\\python.exe -m pip install pymupdf") from None
        limit = max(240, min(int(w), 3000))
        try:
            with fitz.open(str(path)) as doc:
                if page < 1 or page > doc.page_count:
                    raise HTTPException(status_code=404,
                                        detail=f"没有第 {page} 页（共 {doc.page_count} 页）")
                target = doc.load_page(page - 1)
                zoom = limit / max(1.0, float(target.rect.width))
                pix = target.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                data = pix.tobytes("png")
        except HTTPException:
            raise
        except Exception as exc:                               # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"渲染第 {page} 页失败：{exc}") from exc
        return Response(content=data, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=600"})

    @router.get("/api/preview/{file_id}/pdf-hits")
    async def preview_pdf_hits(file_id: int, q: str = Query("", description="要定位的文字"),
                               max_pages: int = 300):
        """在 PDF 里按字面定位文字：第几页、页内哪个位置。

        搜索结果只说明「这个文件里有相关内容」，用户真正要的是「在哪一页、页面哪儿」。
        索引里存的是各页文字拼起来的一整段、页码早丢了，所以这里现读 PDF 的文字层来定位：
        人名、编号、年份这类专有名词最管用（和知识库检索强调的
        「专有名词必须靠字面匹配」是同一个道理）。
        返回的坐标是 0~1 的比例，前端画高亮框时不用关心图片实际多大。

        **2026-10-16 修**：从知识库「来源」点开文件时，传进来的是**整个问题**
        （「李四是谁」），PDF 里当然没有这一整串 ✗ → 界面只会说"没有字面文字"。
        所以现在：整串找不到时，用 `_query_keywords()` 拆出候选词、
        **按长度从长到短**逐个试，用第一个能命中的词 ✓
        （长词最具体：「李四」比滑窗出来的「天齐」准得多），
        并把真正用上的词放在 `used_term` 里告诉前端 ✓。
        """
        query = (q or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="要给 q（要定位的文字）")
        row, path = _indexed_file(file_id)
        if not os.path.isfile(str(path)):
            raise HTTPException(status_code=404, detail="文件当前读不到（网络共享可能未连接）")
        try:
            import fitz                                            # PyMuPDF
        except ImportError:
            raise HTTPException(
                status_code=503,
                detail="按位置定位需要 PyMuPDF："
                       "venv\\Scripts\\python.exe -m pip install pymupdf") from None

        limit = max(1, min(int(max_pages), 2000))
        tried: list[str] = []

        def _locate(term: str) -> dict[str, list[list[float]]]:
            """把一个词在 PDF 里定位一遍，返回 {页号: [框, ...]}"""
            found: dict[str, list[list[float]]] = {}
            scanned = 0
            total_pages = 0
            with fitz.open(str(path)) as doc:
                total_pages = doc.page_count
                scanned = min(total_pages, limit)
                for index in range(scanned):
                    page = doc.load_page(index)
                    rects = page.search_for(term)
                    if not rects:
                        continue
                    width = max(1.0, float(page.rect.width))
                    height = max(1.0, float(page.rect.height))
                    found[str(index + 1)] = [
                        [round(r.x0 / width, 5), round(r.y0 / height, 5),
                         round(r.x1 / width, 5), round(r.y1 / height, 5)]
                        for r in rects[:40]                     # 同一页命中太多也没必要全标
                    ]
                    if len(found) >= 30:                        # 够定位了就打住
                        break
            return {"hits": found, "total": total_pages, "scanned": scanned}

        try:
            # 候选顺序**跟着 query_keywords 的优先级**（实词块"方六/张三"在最前 ✓），
            # 别再按长度重排 ✗ —— 那样两个二字名会被挤到第 15 位以后、被 [:8] 截掉 ✗
            candidates = [query]
            for kw in _query_keywords(query):
                if kw != query and kw not in candidates:
                    candidates.append(kw)
            per_term: list[tuple[str, dict]] = []
            tried_terms: list[str] = []
            for term in candidates[:10]:
                tried.append(term)
                tried_terms.append(term)
                res = _locate(term)
                if res["hits"]:
                    per_term.append((term, res))
                    # 已经有两个词命中就够了（问句里通常就一两个人名）：
                    # 多搜一个词的代价约「页数 × 几毫秒」，值得 ✓
                    if len(per_term) >= 2:
                        break
            result = {"hits": {}, "total": 0, "scanned": 0}
            used = query
            if per_term:
                # 找"同时被最多候选词命中的那一页" ✓ ——
                # 问「方六是张三的什么人」时，两人同现的那页才是答案页 ✓
                page_score: dict[int, int] = {}
                for _term, res in per_term:
                    for p in res["hits"]:
                        page_score[int(p)] = page_score.get(int(p), 0) + 1
                best_page = max(page_score, key=lambda p: (page_score[p], -p))
                winners = [t for t, res in per_term if str(best_page) in res["hits"]]
                used = "、".join(winners) if len(winners) > 1 else winners[0]
                # 命中框**合并**：那一页上两个名字都被黄框标出来 ✓
                merged: dict[str, list[list[float]]] = {}
                total_pages = scanned_pages = 0
                for _term, res in per_term:
                    total_pages = max(total_pages, res["total"])
                    scanned_pages = max(scanned_pages, res["scanned"])
                    for p, boxes in res["hits"].items():
                        merged.setdefault(p, []).extend(boxes)
                result = {"hits": merged, "total": total_pages, "scanned": scanned_pages}
        except HTTPException:
            raise
        except Exception as exc:                               # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"定位失败：{exc}") from exc

        hits = result["hits"]
        pages = sorted(int(p) for p in hits)
        return {"ok": True, "name": row["name"], "query": used,
                "asked": query, "used_term": used, "tried_terms": tried,
                "total_pages": result["total"], "scanned_pages": result["scanned"],
                "first_page": pages[0] if pages else None,
                "hit_pages": pages, "hits": hits,
                "truncated": result["total"] > result["scanned"]}

    @router.get("/api/preview/{file_id}/text")
    async def preview_text(file_id: int, limit: int = 120_000):
        """把正文抽出来给前端显示（文本/代码/Word/Excel/PPT/PDF 都走这里）。"""
        row, path = _indexed_file(file_id)
        if not os.path.isfile(str(path)):
            raise HTTPException(status_code=404, detail="文件当前读不到（网络共享可能未连接）")
        try:
            kind, text = extract.extract(Path(path), cfg.max_file_mb * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"读取正文失败：{exc}") from exc
        text = text or ""
        capped = max(1000, min(int(limit), 1_000_000))
        return {
            "id": file_id,
            "name": row["name"],
            "kind": kind,
            "size": row["size"],
            "text": text[:capped],
            "truncated": len(text) > capped,
            "total_chars": len(text),
        }

    return router
