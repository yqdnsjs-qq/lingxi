"""EmbeddingGemma 2 本地 HTTP 服务。

对外提供：
  GET  /                    网页测试界面
  GET  /health              健康检查
  GET  /v1/models           OpenAI 兼容的模型列表
  POST /v1/embeddings       OpenAI 兼容的嵌入接口（支持多模态扩展）
  POST /embed               原生接口（返回更完整的信息）
  POST /admin/shutdown      优雅退出（仅允许本机调用）

直接运行：  python app\\server.py
"""
from __future__ import annotations

import base64
import logging
import os
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 允许 python app\server.py 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import desktop
    from app.config import Config
    from app.filesearch import create_router as create_search_router
    from app.folderconf import (FolderConfig, clean_subcats, load_categories,
                               subcat_override, union_modality)
    from app.indexer import Indexer
    from app.model import EmbeddingEngine, InputError, ServiceBusy
    from app.settings_api import create_router as create_settings_router
    from app.store import Store
    from app.version import BUILD as BUILD_ID
    from app.version import VERSION as VERSION_ID
else:
    from . import desktop
    from .config import Config
    from .filesearch import create_router as create_search_router
    from .folderconf import (FolderConfig, clean_subcats, load_categories,
                             subcat_override, union_modality)
    from .indexer import Indexer
    from .model import EmbeddingEngine, InputError, ServiceBusy
    from .settings_api import create_router as create_settings_router
    from .store import Store
    from .version import BUILD as BUILD_ID
    from .version import VERSION as VERSION_ID


def effective_modality(cfg: Config, store: Store) -> str:
    """全局模态与各文件夹的需求取并集——文件夹要图片就得加载视觉编码器。

    ⚠ 2026-10-17：**子目录归类也要算进来** ✓ —— 否则「本层只索引文件名、
    子目录「照片」当照片库」时，全局不加载视觉编码器 → 那个子目录一张图都进不去 ✗。
    """
    configs = []
    for folder in store.list_folders(stats=False):
        raw = folder.get("config") or {}
        configs.append(FolderConfig(cfg, raw))
        for _name, _val in clean_subcats(raw.get("subcats")).items():
            # 自定义（dict）或预设归类（字符串）都要算进来 ✓
            _cat, _custom = (("custom", _val) if isinstance(_val, dict)
                             else (str(_val), {}))
            configs.append(FolderConfig(cfg, subcat_override(raw, _cat, _custom)))
    return union_modality(cfg.modality, configs)

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

APP_VERSION = "2.0.0"

log = logging.getLogger("eg2.server")


# ------------------------------------------------------------------ 请求模型
class EmbeddingRequest(BaseModel):
    input: Any = Field(..., description="文本 / 文本数组 / 多模态对象数组")
    model: str | None = None
    encoding_format: str = "float"
    dimensions: int | None = None
    truncate_dim: int | None = None
    prompt_name: str | None = None
    task: str | None = None
    normalize: bool | None = None
    batch_size: int | None = None
    user: str | None = None


def _floats_to_base64(values: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def _normalize_input(payload: Any) -> list[Any]:
    if payload is None:
        raise InputError("input 不能为空")
    if isinstance(payload, (str, dict)):
        return [payload]
    if isinstance(payload, (list, tuple)):
        if not payload:
            raise InputError("input 数组不能为空")
        return list(payload)
    raise InputError(f"不支持的 input 类型：{type(payload).__name__}")


def _close_ui_now() -> None:
    """把**界面窗口**关掉（本应用自己的窗口 ✓ 不碰用户主浏览器 ✓）。

    为什么需要：`window.close()` 关不掉 `--app` 打开的窗口 ✗
    （项目主实测"不会自动关闭"）→ 由服务端代劳 ✓。
    ⚠ 一定要在**服务退出流程里同步**调用（`main()` 的 finally）✓ ——
      用 threading.Timer 延迟关窗会在 uvicorn 退出、进程结束时被掐掉 ✗
      （实测：服务停了，窗口还留着 ✗）。
    """
    root = Path(__file__).resolve().parent.parent
    try:
        import subprocess as _sp

        py = root / "venv" / "Scripts" / "python.exe"
        if not py.is_file():
            py = root / "venv" / "bin" / "python"
        script = root / "app" / "close_ui.py"
        if py.is_file() and script.is_file():
            _sp.run([str(py), str(script), "--wait", "1.5"],
                    cwd=str(root), capture_output=True, timeout=60)
    except Exception as exc:                                      # noqa: BLE001
        log.warning("关界面窗口失败（不影响停止服务）：%s", exc)


def create_app(cfg: Config, engine: EmbeddingEngine, store: Store | None = None) -> FastAPI:
    app = FastAPI(
        title="EmbeddingGemma 2 本地服务",
        version=APP_VERSION,
        description="Google EmbeddingGemma 2 的多模态嵌入服务（文本 / 图片 / 音频 / 视频）"
                    "＋ 本地文件语义搜索",
    )
    app.state.cfg = cfg
    app.state.engine = engine
    app.state.server = None
    static_dir = Path(__file__).resolve().parent / "static"

    # ---- 文件搜索：索引库 + 索引器 + 接口 ----
    store = store or Store(cfg)
    store.check_dim()
    # 类别表（A+B，2026-10-18）：从本机 json 读"我改过的默认口径 / 我自己加的类别" ✓
    # 文件不存在 = 完全用出厂默认 ✓（老行为不变 ✓）
    try:
        _cats = load_categories(Path(cfg.db_path).parent / "folder_categories.json")
        log.info("文件夹类别表已加载：%d 类（其中自定义 %d 个）",
                 len(_cats), sum(1 for c in _cats.values() if not c.get("builtin")))
    except Exception as _exc:                                 # noqa: BLE001
        log.warning("加载文件夹类别表失败（用出厂默认）：%s", _exc)
    engine.modality_provider = lambda: effective_modality(cfg, store)
    indexer = Indexer(cfg, store, engine)
    app.state.store = store
    app.state.indexer = indexer
    app.include_router(create_search_router(cfg, store, engine, indexer))
    app.include_router(create_settings_router(cfg, store, engine, indexer))

    # 聊天标签页（会话保存 + 联网搜索）—— 注意缩进：这段在函数内部
    try:
        from app.chat_api import create_router as create_chat2_router
    except ImportError:
        from .chat_api import create_router as create_chat2_router
    app.include_router(create_chat2_router(cfg, store, engine))

    # 账号与会话（多人使用 P1）：本阶段只**提供能力**，不拦截既有接口 ——
    # 拦截要和登录界面一起上（P2/P3），否则升级后会被自己锁在外面。
    try:
        from app.auth_api import create_router as create_auth_router
    except ImportError:                                       # pragma: no cover
        from .auth_api import create_router as create_auth_router
    app.include_router(create_auth_router(cfg))

    # 登录守卫（多人 P2-b）：**默认关闭**，只有设了环境变量 EG2_ENFORCE_LOGIN=1
    # 才会拦"未登录访问业务接口"。守卫自己也会再查一次开关（纵深防御）。
    # 之所以默认关：开 401 会连带挡住同步任务 / Dify / 验收脚本（它们不带 Cookie），
    # 一刀切会把系统搞坏，而且不容易发现。
    try:
        from app.auth_guard import LoginGuard, enabled as _guard_on
    except ImportError:                                       # pragma: no cover
        from .auth_guard import LoginGuard, enabled as _guard_on
    if _guard_on():
        app.add_middleware(LoginGuard, db_path=cfg.db_path)
        log.warning("登录守卫已启用：未登录将无法访问业务接口")

    # 「立即同步」按钮的接口（在界面上点一下就跑一次同步，不用等 15 分钟）
    try:
        from app.sync_api import create_router as create_sync_router
    except ImportError:                                       # pragma: no cover
        from .sync_api import create_router as create_sync_router
    app.include_router(create_sync_router(cfg))

    # 管理界面：文件夹「共享 / 私有」设置（多人 P3）
    try:
        from app.admin_api import create_router as create_admin_router
    except ImportError:                                       # pragma: no cover
        from .admin_api import create_router as create_admin_router
    app.include_router(create_admin_router(cfg))

    # 预览/原图接口的可见性守卫（多人 P2-b）：**和登录守卫不同，它默认就开着** ✓
    # 因为它是"数据不该被看到"的最后一道 —— 搜索和问答都挡住了，预览面不能漏 ✗。
    # 它只拦"确实落在私有文件夹里的"请求，自己出错时放行（可用性优先 ✓）。
    try:
        from app.preview_guard import PreviewGuard
    except ImportError:                                       # pragma: no cover
        from .preview_guard import PreviewGuard
    app.add_middleware(PreviewGuard, db_path=cfg.db_path)

    # 「页面还在用吗」的心跳记录（给"关掉界面就顺手停服务"用，见 /api/ui-leaving）：
    # 任何请求进来都刷新一下时间 ✓ —— 用来分辨"关窗口"和"刷新页面"：
    #   关闭窗口 → 之后 5 秒内没有任何请求 → 判定为真的关了 → 停服务 ✓
    #   刷新页面 → 新页面立刻就来请求了 → 撤销停止 ✓（绝不会误停 ✗）
    @app.middleware("http")
    async def _touch_last_seen(request: Request, call_next):   # noqa: ANN001
        app.state.last_seen = time.time()
        response = await call_next(request)
        # 静态页（尤其 search.html）**禁用强缓存** ✓：不然改了前端，
        # 旧窗口/新窗口都可能继续用缓存里的老页面 ✗
        # （2026-10-17 实测踩到：界面窗口加载的是没有心跳代码的旧页面，
        #   于是"关掉界面停服务"完全不生效 ✗）
        path = request.url.path
        if path.startswith("/static/") or path in ("/search", "/", "/static"):
            # ★ 2026-10-22：这里以前**无条件**盖成 no-cache，把下面 _NoCacheStatic
            #   给带哈希文件的 `immutable` 一起盖掉了 —— 结果每次开页面都把
            #   325 KB 的 app.js 重新下一遍（外网走 Cloudflare 时最明显）✗
            #   现在分开处理：带内容哈希的给长缓存（名字里就有内容指纹，改了就换名），
            #   其余（search.html / settings.js）继续保持 no-cache ✓
            _nm = path.rsplit("/", 1)[-1]
            try:
                _is_hashed = bool(_HASHED_RE.search(_nm))
            except Exception:                                    # noqa: BLE001
                _is_hashed = False
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if _is_hashed
                else "no-cache, must-revalidate")
        return response

    # ---- 「界面还在吗」的心跳 + 关掉界面就停服务（2026-10-17 项目主要求）----
    # 为什么不用"离开时发一次通知"当判据：实测**不可靠** ✗ ——
    # 关窗瞬间还有别的请求漏进来，就被判成"刷新"了（日志："界面刷新（不是关闭）"✗）。
    # 心跳判据只认**界面自己发的**那一条 ✓，别的请求干扰不了 ✓；
    # 而且界面崩溃 / 被强杀也能识别（心跳断了 ✓）。
    # ⚠ **只在桌面版启用**：服务器是长期服务，绝不能因为"没人开着界面"就自己停 ✗。
    # ⚠ 2026-10-17 项目主明确：**不要"一会没人用就自动关闭"** ✗ ——
    #    所以这里**不做**"服务起来后没人开界面就停"那种判断 ✓：
    #    没人开界面时服务就一直待命（等你双击 start.bat 来用）✓。
    #    唯一的停止条件：**界面连上过、然后心跳消失**（= 你手动关掉了界面窗口 ✓）。
    def _is_desktop() -> bool:
        root_dir = Path(__file__).resolve().parent.parent
        return (root_dir / "run" / "deploy" / "sync_db.py").is_file()

    app.state.ui_alive = 0.0
    # 心跳空闲阈值（可用环境变量调，测试时用小值免得等 ✓）
    #
    # ⚠ 2026-10-18 修正：原来默认 35 秒，**太短** ✗ —— 实测把服务"自杀"了：
    #   16:35:50 收到一次心跳 → 16:36:28（38 秒后）判定"界面消失"→ 停服务
    #   而当时界面窗口明明还开着（pid 没变）✗
    # 原因有两类：
    #   · 服务重启期间界面是断连的，心跳发不出去（重启要 30~60 秒）
    #   · 浏览器会**节流后台标签页的定时器**（切走/最小化时可能一分钟才跑一次）
    # 所以：① 阈值提到 120 秒 ② 判定后再**确认一轮**（真关了才停）✓
    #
    # ⚠⚠ 2026-10-19 再次修正（项目主："问答时服务崩了"）：
    #   日志实证：14:09:06 心跳停了 121 秒 → 14:09:26 确认 → **把自己停了** ✗
    #   而当时正在做一次长问答（上下文 16411 token，超过模型上限 8192，很慢）。
    #   界面在等回答（或者标签被浏览器节流）→ 心跳断了 → 被误判成"界面关了" ✗
    #   ★ 所以加一条**铁律**：**服务正在忙时，绝不自停** ✓
    #     忙的判据 = 索引在跑 / 最近有请求（说明有人在用）✓
    try:
        idle_limit = float(os.environ.get("EG2_UI_IDLE", "") or 300)
    except ValueError:
        idle_limit = 300.0
    # 「最近有请求」算忙的窗口（问答可能几分钟才回，所以给足 ✓）
    busy_window = float(os.environ.get("EG2_UI_BUSY", "") or 900)

    def _busy_reason() -> str:
        """服务"正忙"的原因（非空 = 忙 = 不许自停）。"""
        try:
            idx = getattr(app.state, "indexer", None)
            if idx is not None and (idx.status() or {}).get("running"):
                return "正在索引"
        except Exception:                                        # noqa: BLE001
            pass
        try:
            if int(getattr(app.state, "kb_busy", 0) or 0) > 0:
                return "正在问答"
        except Exception:                                        # noqa: BLE001
            pass
        try:
            seen = float(getattr(app.state, "last_seen", 0.0) or 0.0)
            if seen:
                gap = time.time() - seen
                if gap < busy_window:
                    return f"%.0f 秒前还有请求" % gap
        except Exception:                                        # noqa: BLE001
            pass
        return ""

    def _watch_ui() -> None:
        time.sleep(30)                        # 启动后先给界面 30 秒上线 ✓
        while True:
            try:
                last = float(getattr(app.state, "ui_alive", 0.0) or 0.0)
                now = time.time()
                if last and (now - last > idle_limit):
                    # ★ 忙就不许停（2026-10-19：问答时把服务自己停了 ✗）
                    why = _busy_reason()
                    if why:
                        log.info("界面心跳停了 %.0f 秒，但服务正忙（%s）→ 不停止 ✓",
                                 now - last, why)
                        time.sleep(10)
                        continue
                    # ② 再确认一轮：真关了才会一直没有心跳 ✓
                    log.info("界面心跳停了 %.0f 秒，再等 20 秒确认…", now - last)
                    time.sleep(20)
                    again = float(getattr(app.state, "ui_alive", 0.0) or 0.0)
                    if again > last + 0.001:
                        log.info("界面又发心跳了（刚才只是断连/被节流）→ 服务继续")
                        continue
                    why2 = _busy_reason()                # 确认期间可能又开始忙了 ✓
                    if why2:
                        log.info("确认时发现服务正忙（%s）→ 不停止 ✓", why2)
                        continue
                    log.info("确认界面已经消失（心跳停了 %.0f 秒）→ 顺手停止服务",
                             time.time() - again)
                    srv = app.state.server
                    if srv is not None:
                        srv.should_exit = True
                    return
            except Exception as exc:                              # noqa: BLE001
                log.debug("界面心跳检查出错：%s", exc)
            time.sleep(5)

    if _is_desktop():
        threading.Thread(target=_watch_ui, name="eg2-ui-watch", daemon=True).start()

    # 「哪边点同步都能同步」：本机轮询服务器上的同步请求（服务器上自动不启动 ✓）
    try:
        from app.sync_poller import start as _start_sync_poller
    except ImportError:                                       # pragma: no cover
        from .sync_poller import start as _start_sync_poller
    if _start_sync_poller(cfg):
        log.info("已启用：服务器上点「同步」时本机会自动执行")

    # 换库时把这里的引用也换掉。
    # engine.modality_provider 那个闭包读的就是本作用域的 store；
    # Indexer 和 app.state 也各持一份 —— 漏掉任何一个，换库后那处就会用
    # 已经关闭的旧连接（详见 app/dbreload.py）。
    # 双重写法：server.py 既会被当模块导入，也会被直接执行
    # （python app/server.py）—— 后者没有包上下文，相对导入会报
    # "attempted relative import with no known parent package"（实测踩过）。
    try:
        from app.dbreload import register as _register_reload
    except ImportError:                                       # pragma: no cover
        from .dbreload import register as _register_reload

    def _rebind_db(new_store) -> None:
        nonlocal store, indexer
        store = new_store
        indexer.store = new_store
        app.state.store = new_store
        app.state.indexer = indexer

    _register_reload("app", _rebind_db)

    from fastapi.staticfiles import StaticFiles

    # ★ 2026-10-20：静态页一律 **no-cache** ✓
    #   原因：改了 search.html 之后，用户浏览器还拿着旧版（点了没反应、
    #   新加的卡片不出现、"一直显示正在读取…"），每次都要手动 Ctrl+F5 ✗
    #   （项目主实测踩到：加了「隐私文件夹」「选文件」两个卡，他那边完全看不到）
    #   这些文件都只有几十~几百 KB，关掉缓存的开销可以忽略 ✓
    _HASHED_RE = __import__("re").compile(r"^app\.[0-9a-f]{6,32}\.(js|css)$")

    class _NoCacheStatic(StaticFiles):
        """静态资源缓存策略（在 ASGI 消息层改头，见下面注释）。

        · 文件名带内容哈希的 `app.<hex>.js|css` → 缓存一年、immutable ✓
          （内容一变文件名就变，绝不会读到旧代码）
        · 其他（search.html、settings.js…）→ no-cache，保证每次都是新版 ✓
        """

        async def get_response(self, path, scope):               # noqa: ANN001
            """★ 2026-10-22：**旧哈希文件被删掉了**不要回空 404。

            每改一次前端，`run/rehash_static.py` 会把旧的 `app.<hash>.js` 删掉。
            如果浏览器手里还拿着上一秒的 HTML（或者刚好在改名那一瞬间刷新），
            它去要的旧文件名就 404 了 —— 而 404 的 body 是**空的**，
            浏览器把空脚本当语法错误，页面上就弹出
            「脚本错误：Uncaught SyntaxError: Unexpected end of input /search:1」
            （项目主 2026-10-22 真碰到了，完全看不出是"文件换名"）✗

            现在遇到这种"过期的哈希文件"，回一段**能正常解析的小脚本**：
            自动硬刷新一次（用 sessionStorage 防死循环），刷不到再提示手动 Ctrl+F5 ✓
            """
            from starlette.exceptions import HTTPException as _HTTPExc

            try:
                return await super().get_response(path, scope)
            except _HTTPExc as exc:
                if getattr(exc, "status_code", 0) != 404:
                    raise
                if not _HASHED_RE.search(str(path).rsplit("/", 1)[-1]):
                    raise                       # 不是哈希文件 → 照旧 404，别掩盖真问题
                from starlette.responses import Response

                if str(path).endswith(".css"):
                    # 样式表给空内容就行：页面会靠自动刷新重载新样式
                    return Response(
                        "/* 静态样式已更新，页面正在重新加载… */",
                        media_type="text/css",
                        headers={"cache-control": "no-store",
                                 "cdn-cache-control": "no-store"})
                js = (
                    "(function(){try{"
                    "var k='eg2-stale-asset';"
                    "if(!sessionStorage.getItem(k)){"
                    "sessionStorage.setItem(k,'1');"
                    "location.reload();return;}"
                    "}catch(e){}"
                    "console.warn('[灵犀] 页面用的是旧版静态文件，已经不存在了。"
                    "请按 Ctrl+F5 强制刷新。');"
                    "})();\n"
                )
                log.warning("请求了已被替换的静态文件：%s（已回一段自动刷新脚本）", path)
                return Response(js, media_type="text/javascript; charset=utf-8",
                                headers={"cache-control": "no-store",
                                         "cdn-cache-control": "no-store"})

        async def __call__(self, scope, receive, send):          # noqa: ANN001
            _p = str(scope.get("path") or "").lstrip("/")
            _name = _p.rsplit("/", 1)[-1]
            _hashed = bool(_HASHED_RE.search(_name))
            # ⚠ no-cache 会被 Cloudflare 的 Browser Cache TTL 改成 max-age=14400 ✗
            #   用 no-store（CF 明确不缓存）+ CDN-Cache-Control 双保险 ✓
            _cc = ("public, max-age=31536000, immutable" if _hashed
                   else "no-store, no-cache, must-revalidate, max-age=0")

            async def _send(message):                            # noqa: ANN001
                if message.get("type") == "http.response.start":
                    # ★ 必须在这一层改：`FileResponse` 的 raw_headers 早就定好了，
                    #   在 get_response 里改 resp.headers 不保证生效 ✗（踩过）
                    from starlette.datastructures import MutableHeaders

                    hdrs = MutableHeaders(scope=message)
                    hdrs["cache-control"] = _cc
                    if not _hashed:
                        hdrs["cdn-cache-control"] = "no-store"   # Cloudflare 认这个
                await send(message)

            await super().__call__(scope, receive, _send)

    app.mount("/static", _NoCacheStatic(directory=str(static_dir)), name="static")

    # ★ 2026-10-21 外网优化：gzip 压缩
    #   外网走 Cloudflare 隧道（节点在欧洲），367 KB 的 search.html 要几十秒 ✗
    #   文本压到 ~1/5 后，首屏时间能明显下降 ✓
    try:
        from starlette.middleware.gzip import GZipMiddleware

        app.add_middleware(GZipMiddleware, minimum_size=1024)
        log.info("  [优化] 已启用 gzip 压缩（>1KB 的文本响应）")
    except Exception as exc:                                     # noqa: BLE001
        log.warning("  启用 gzip 失败（不影响主服务）：%s", exc)

    @app.exception_handler(InputError)
    async def _input_error(_request: Request, exc: InputError):
        return JSONResponse(status_code=400, content={"error": {"message": str(exc), "type": "invalid_request_error"}})

    @app.exception_handler(ServiceBusy)
    async def _busy(_request: Request, exc: ServiceBusy):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    async def index():
        """首页按机器分流（项目主 2026-10-21）。

        · 本机（建库机）：嵌入服务调试页 `index.html` —— 开发/调试要用 ✓
        · 服务器：**不暴露调试页**，直接送到搜索页（未登录会被守卫带去登录）✓
        """
        from fastapi.responses import RedirectResponse

        is_builder = True
        try:
            from app.filesearch import _can_index_here

            is_builder = bool(_can_index_here())
        except Exception:                                        # noqa: BLE001
            is_builder = (root / "run" / "deploy" / "sync_db.py").is_file()

        if not is_builder:
            return RedirectResponse("/static/search.html", status_code=302)
        page = static_dir / "index.html"
        if page.exists():
            return FileResponse(page, media_type="text/html; charset=utf-8")
        return JSONResponse({"service": "EmbeddingGemma 2", "docs": "/docs"})

    # ★ 服务器上把接口文档关掉（公网不该看到 /docs、/openapi.json）
    #   ⚠ 不能把 app.openapi_url 置 None —— 路由早就注册好了，生成文档时
    #     拿不到 schema 会直接 500 ✗（实测栽过）。改成**显式返回 404**。
    try:
        from app.filesearch import _can_index_here as _cih

        if not _cih():
            # 直接从路由表移除（加覆盖路由不行：自动生成的 docs 会抢先匹配 ✗）
            _drop = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
            before = len(app.router.routes)
            app.router.routes = [
                r for r in app.router.routes
                if getattr(r, "path", "") not in _drop
            ]
            print(f"  [外网] 已关闭接口文档：移除 {before - len(app.router.routes)} 条路由")
    except Exception as exc:                                     # noqa: BLE001
        print(f"  [外网] 关闭接口文档失败（不影响主服务）：{exc}")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": VERSION_ID,    # ★ 2026-10-23：版本号（小升级也要能看出来）
            "build": BUILD_ID,
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - START_TIME, 1),
            "port": cfg.port,
            # 定位/打开文件能不能用，看这一项就知道
            "desktop": desktop.diagnose(),
            **(engine.info() if engine.model is not None else {"loaded": False}),
        }

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [
                {
                    "id": cfg.repo_id,
                    "object": "model",
                    "created": int(START_TIME),
                    "owned_by": "google",
                    "root": cfg.repo_id,
                }
            ],
        }

    @app.post("/v1/embeddings")
    async def embeddings(req: EmbeddingRequest):
        try:
            items = _normalize_input(req.input)
        except InputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        dim = req.truncate_dim or req.dimensions
        prompt = req.prompt_name or req.task
        try:
            vectors = engine.encode(
                items,
                prompt_name=prompt,
                dim=dim,
                normalize=req.normalize,
                batch_size=req.batch_size,
            )
        except InputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("编码失败")
            raise HTTPException(status_code=500, detail=f"编码失败：{exc}") from exc

        fmt = (req.encoding_format or "float").lower()
        data = []
        for i, vec in enumerate(vectors):
            payload: Any = _floats_to_base64(vec) if fmt == "base64" else vec
            data.append({"object": "embedding", "index": i, "embedding": payload})
        tokens = engine.count_tokens(items)
        return {
            "object": "list",
            "data": data,
            "model": req.model or cfg.repo_id,
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        }

    @app.post("/embed", include_in_schema=True)
    async def embed_native(req: EmbeddingRequest):
        """原生接口：返回维度、耗时、前缀等额外信息，便于调试。"""
        try:
            items = _normalize_input(req.input)
            dim = req.truncate_dim or req.dimensions
            started = time.perf_counter()
            vectors = engine.encode(
                items,
                prompt_name=req.prompt_name or req.task,
                dim=dim,
                normalize=req.normalize,
                batch_size=req.batch_size,
            )
        except InputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("编码失败")
            raise HTTPException(status_code=500, detail=f"编码失败：{exc}") from exc
        elapsed = time.perf_counter() - started
        return {
            "model": cfg.repo_id,
            "count": len(vectors),
            "dimension": len(vectors[0]) if vectors else 0,
            "prompt_name": engine.resolve_prompt(req.prompt_name or req.task),
            "normalized": cfg.normalize if req.normalize is None else req.normalize,
            "elapsed_seconds": round(elapsed, 4),
            "embeddings": vectors,
        }

    def _close_ui_soon(delay: float = 1.0) -> None:
        """过一小会儿把**界面窗口**关掉（点「关闭」/ 关掉界面后停服务时用）。

        为什么要服务端来关：`window.close()` 关不掉 `--app` 模式打开的窗口 ✗
        （实测：前端调了它，窗口照样留着 —— 项目主反馈"不会自动关闭"）。
        而 `app\\close_ui.py` 认的是"命令行里带专用配置 .browser 的进程" ✓，
        只关本应用的窗口 ✓（不会碰用户主浏览器 ✓）。
        延迟一点是为了让前端先把"服务已停止"那句话渲染出来 ✓。
        """
        def _do() -> None:
            try:
                import subprocess as _sp

                py = ROOT / "venv" / "Scripts" / "python.exe"
                if not py.is_file():
                    py = ROOT / "venv" / "bin" / "python"
                script = ROOT / "app" / "close_ui.py"
                if py.is_file() and script.is_file():
                    _sp.run([str(py), str(script), "--wait", "1.5"],
                            cwd=str(ROOT), capture_output=True, timeout=60)
            except Exception as exc:                              # noqa: BLE001
                log.warning("关界面窗口失败（不影响停止服务）：%s", exc)

        threading.Timer(max(0.0, delay), _do).start()

    @app.post("/admin/shutdown", include_in_schema=False)
    async def shutdown(request: Request):
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(status_code=403, detail="只允许本机调用")
        log.info("收到关闭请求，正在退出…")
        server = app.state.server
        if server is not None:
            server.should_exit = True
        return {"status": "shutting-down"}

    @app.post("/api/ui-alive", include_in_schema=False)
    async def ui_alive(request: Request):
        """界面心跳（前端每 10 秒一次）—— 界面关了就不再有心跳 ✓

        只在**桌面版**有意义（服务器上不启动监控线程 ✓）。
        判据只认这一条，其它请求（刷新、同步轮询、外部调用）都不影响判断 ✓
        """
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            return {"ok": False}
        if not getattr(app.state, "ui_alive", 0.0):
            log.info("收到界面心跳（界面在线）")                 # 每次进程只说一次，免得刷屏
        app.state.ui_alive = time.time()
        return {"ok": True}

    @app.post("/api/ui-leaving", include_in_schema=False)
    async def ui_leaving(request: Request):
        """界面要关了（浏览器发 sendBeacon 通知）。

        2026-10-17 项目主要求：**窗口的叉关掉界面时，服务也该跟着停** ✓
        —— 不然"界面没了、服务还在、还留个黑框，还得再点一次" ✗。

        难点是**别把刷新页面当成关闭** ✗（那会误停服务）。
        做法：记下"离开时刻"，**等 5 秒**再看有没有新请求
        （刷新的话新页面立刻就来请求了 → 撤销 ✓；只有真的关了才停 ✓）。
        """
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            return {"ok": False}                              # 只对本机界面生效 ✓
        app.state.last_seen = 0.0                             # 从"离开"这一刻起重新计时
        stamp = time.time()

        def _maybe_stop() -> None:
            time.sleep(5.0)
            seen = float(getattr(app.state, "last_seen", 0.0) or 0.0)
            if seen > stamp:                                  # 期间有请求 → 是刷新，不是关闭 ✓
                log.info("界面刷新（不是关闭），服务继续运行")
                return
            log.info("界面已关闭 → 顺手停止服务")
            try:
                server = app.state.server
                if server is not None:
                    server.should_exit = True
            except Exception as exc:                          # noqa: BLE001
                log.warning("界面关闭后停服务失败：%s", exc)

        threading.Thread(target=_maybe_stop, name="eg2-ui-leaving",
                         daemon=True).start()
        return {"ok": True}

    return app


START_TIME = time.time()


def _setup_logging(cfg: Config) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _apply_pending_restore() -> None:
    """如果上次在界面上点了「还原到这个备份」，在这里完成替换。

    为什么放到启动时做：服务运行时 SQLite 正打开着 index.db，
    直接覆盖会损坏数据库。启动时还没打开，最安全。
    """
    import shutil
    import time
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    idx_dir = root / "data" / "index"
    pending = idx_dir / "pending-restore.txt"
    db = idx_dir / "index.db"
    if not pending.exists():
        return
    name = pending.read_text(encoding="utf-8").strip()
    src = idx_dir / name
    if not src.is_file():
        log.warning("要还原的备份不存在：%s，忽略", name)
        pending.unlink()
        return
    # 先把当前库备份一份，万一还原错了还能回来
    safety = idx_dir / f"index-backup-{time.strftime('%Y%m%d-%H%M%S')}-还原前.db"
    try:
        if db.is_file():
            shutil.copy2(db, safety)
        tmp = idx_dir / "index.db.restoring"
        shutil.copy2(src, tmp)
        tmp.replace(db)
        log.info("已还原数据库：%s（还原前的库备份为 %s）", name, safety.name)
    except Exception as exc:                                    # noqa: BLE001
        log.error("还原失败：%s", exc)
    finally:
        pending.unlink(missing_ok=True)


def main() -> int:
    _apply_pending_restore()
    cfg = Config()
    cfg.ensure_dirs()
    _setup_logging(cfg)

    header = f"宇之语 · 灵犀 本地服务  v{APP_VERSION}"
    log.info("=" * 60)
    log.info(header)
    log.info("模型目录: %s", cfg.model_dir)
    log.info("=" * 60)

    info = desktop.diagnose()
    log.info("桌面交互环境：完整性级别=%s 管理员=%s 会话=%s %s",
             info["integrity"], info["elevated"], info["session"] or "?",
             "（定位/打开文件可用）" if info["desktop_ok"] else
             "（定位/打开文件可能失败：进程权限不足以操作桌面资源管理器）")

    log.info("正在加载模型（首次加载约 10-40 秒）…")
    engine = EmbeddingEngine(cfg)
    store = None
    try:
        store = Store(cfg)
        store.check_dim()
        engine.modality_provider = lambda: effective_modality(cfg, store)
        log.info("已加载索引库，按各文件夹需求计算需要加载的模态…")
        # ★ 2026-10-27：后台预热"文件夹在不在"的缓存。
        #   网络盘（NAS）上 stat 一次可能几秒，19 个串起来十几秒 ——
        #   开机就预热，第一次打开文件夹页就不用干等 ✓
        def _warm_exists() -> None:
            try:
                store.list_folders(stats=False)     # 一次性把所有路径的 exists 缓存填上
                log.info("文件夹可用性缓存已预热")
            except Exception as exc:                 # noqa: BLE001
                log.warning("预热文件夹缓存失败（不影响使用）：%s", exc)

        threading.Thread(target=_warm_exists, daemon=True).start()
    except Exception:  # noqa: BLE001
        log.exception("索引库初始化失败，文件搜索可能不可用")
    try:
        engine.load()
    except Exception as exc:  # noqa: BLE001
        log.error("模型加载失败：%s", exc)
        return 2

    app = create_app(cfg, engine, store)

    # ---- Windows：**必须在 import uvicorn 之前**把事件循环策略换成 Selector ----
    # 2026-10-18 深挖发现：策略写在 `import uvicorn` 之后是**不够**的 ✗ ——
    #   uvicorn 在导入/创建 Config 时就会定下 loop 工厂，之后再 set_event_loop_policy()
    #   只能影响"之后新建"的 loop，于是退出阶段仍有 Proactor 的传输在跑 →
    #     16:15:13  ERROR asyncio: Exception in callback BaseProactorEventLoop._start_serving
    #                AssertionError / h11 LocalProtocolError ... state=CLOSED
    #   这次崩溃发生在"心跳消失 → 顺手停服务"的**退出过程中** ✗
    # 所以：① 先切策略 ② import uvicorn ③ Config(loop="asyncio") 显式指定 ✓
    if sys.platform == "win32":
        try:
            import asyncio

            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            log.info("已切换为 Selector 事件循环（在 import uvicorn 之前，防止退出阶段崩溃）")
        except Exception as exc:                                  # noqa: BLE001
            log.warning("切换事件循环失败（继续用默认）：%s", exc)

    import uvicorn

    # ---- Windows：换用 Selector 事件循环，避免"客户端中途断开"把整个进程打崩 ----
    # 实测崩溃（2026-10-17，项目主反馈"改排除规则会闪退"）：
    #   界面里点「预览排除规则」是个长请求（要遍历整个文件夹，NAS 上很慢），
    #   用户等的时候关掉弹窗 / 刷新 → 连接被客户端断开 →
    #     ERROR asyncio: Exception in callback ..._loop_reading  → AssertionError
    #     h11._util.LocalProtocolError: can't handle event type Response
    #                                 when role=SERVER and state=CLOSED
    #   → **服务进程直接退出** ✗（Windows 的 Proactor 处理这种半关闭连接有已知缺陷）
    # Selector 稳得多 ✓，而且本项目不用 asyncio 的子进程/管道，
    # 换过去没有副作用 ✓（关闭窗口/索引都是用同步 subprocess ✓）。

    config = uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        access_log=False,
        timeout_graceful_shutdown=5,
        loop="asyncio",          # 明确就用 asyncio 的策略（不要让 uvicorn 自己挑 auto）✓
    )
    server = uvicorn.Server(config)
    app.state.server = server

    pid_file = cfg.run_dir / "server.pid"
    pid_file.write_text(str(os.getpid()), encoding="ascii")

    # 预热一次，提前把 CUDA kernel 编译掉，避免第一次请求很慢
    try:
        engine.encode(["预热"], prompt_name="none")
        log.info("预热完成")
    except Exception as exc:  # noqa: BLE001
        log.warning("预热失败（不影响使用）：%s", exc)

    url = f"http://{'127.0.0.1' if cfg.host in ('0.0.0.0', '::') else cfg.host}:{cfg.port}/"
    log.info("服务已就绪：%s", url)
    log.info("嵌入接口：%sv1/embeddings", url)
    log.info("文件搜索：%ssearch", url)
    log.info("接口文档：%sdocs", url)
    log.info("停止服务：双击 stop.bat")

    # 启动时增量扫描已添加的文件夹（只处理新增/改动过的文件）
    if cfg.auto_scan_on_start:
        folders = app.state.store.list_folders()
        if folders:
            log.info("开始增量扫描 %d 个文件夹…", len(folders))
            app.state.indexer.start()
    try:
        server.run()
    finally:
        # ⚠ 2026-10-18 撤掉"服务退出时关界面窗口" ✗
        # 上一轮为了配合"关掉界面 → 停服务"，让它在退出时顺手关界面窗口。
        # 结果：**我每次重启服务**（重启脚本、部署脚本）也走这条退出流程 →
        # 把项目主正开着的界面关掉了 ✗ → 他看到"界面没了、黑框还在"✗
        # （项目主为此第二次投诉同一现象）。
        # 现在回到"谁也别动界面窗口"：
        #   · 用户关界面 → 窗口本来就没了，不需要服务端再关 ✓
        #   · stop.bat / 重启 → 界面留着，前端会自己显示"服务已停止"提示 ✓
        # 界面窗口只在 start.bat 打开、由用户自己关 ✓。
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        log.info("服务已停止（界面窗口保持原样，不会被关）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())