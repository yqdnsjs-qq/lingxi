"""OCR / PDF 解析引擎的统一入口。

三个引擎各有所长，**做成可选的**（项目主 2026-10-19 要求）：
  · mineru —— 结构化 Markdown + 自动提取插图（最强，适合文字版 PDF/表格/版面）
  · ppocr  —— PP-OCRv6：快、轻、带文字坐标（适合纯图片、扫描页、需要定位时）
  · rapid  —— RapidOCR：最轻，纯 CPU 不占显存（兜底）

三者都是**独立环境里的桥**，通过子进程调用，输出统一 JSON：
  {"ok":true,"engine":"..","pages":[{"file":"..","text":"..","lines":[{"text","score","box"}]}]}

⚠ 为什么用子进程而不是直接 import：
  这些引擎的依赖（paddle / onnxruntime / torch）又大又重，混进本项目的 venv
  会把现有依赖搞乱；而且它们的显存占用要能**独立回收** ✓
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path

log = logging.getLogger("eg2.ocr")


_INI_CACHE: dict = {"at": 0.0, "vals": {}}
_INI_TTL = 10.0          # config.ini 缓存 10 秒（界面上改完很快就能生效）


def _ini_ocr() -> dict:
    """读 config.ini 的 `[ocr]` 段（缓存 10 秒，避免每次调用都读盘）。"""
    import time as _t

    now = _t.time()
    if _INI_CACHE["vals"] and now - _INI_CACHE["at"] < _INI_TTL:
        return _INI_CACHE["vals"]
    vals: dict = {}
    try:
        import configparser

        from .config import Config

        cp = configparser.ConfigParser(interpolation=None)
        cp.read(Config().path, encoding="utf-8")
        if cp.has_section("ocr"):
            vals = {k.strip().lower(): (v or "").strip()
                    for k, v in cp.items("ocr")}
    except Exception:                                            # noqa: BLE001
        vals = {}
    _INI_CACHE.update({"at": now, "vals": vals})
    return vals


def _p(env: str, ini_key: str, default: str) -> Path:
    """取引擎路径。三级优先：config.ini（界面填的）> 环境变量 > 默认值。

    `ini_key` 是 config.ini `[ocr]` 段里的键名，例如 `rapid_python`。
    `env` 是兼容用的环境变量名，例如 `EG2_OCR_RAPID_PY`。

    界面上填了就用界面的 ✓（这是主路径）
    没填但有环境变量就用环境变量 ✓（兼容 ocr-env.bat）
    都没有就用默认值 ✓（开发机的 D:/GJ/...，保证本机原样可用）
    """
    v = (_ini_ocr().get(ini_key) or "").strip()
    if v:
        return Path(v)
    v = (os.environ.get(env) or "").strip()
    if v:
        return Path(v)
    return Path(default)


def invalidate_cache() -> None:
    """界面改完路径后调一下，让 _ini_ocr() 立刻重新读。"""
    _INI_CACHE.update({"at": 0.0, "vals": {}})

# ============================================================ 显存互斥（重要）
# 项目主 2026-10-19：
#   "全力跑时，OCR 与打标同时进行吗？如果同时使用本地方案的话，不是要撞显存？
#    这个需要给个顺序进行才行；如果调用 API 我想同时进行倒是影响不大"
#
# 做法：**所有要用显存的活，都必须进这把锁** ✓
#   · 本地 OCR（MinerU / PP-OCRv6 GPU）→ 进锁
#   · 本地模型打标（VL 模型）→ 进锁
#   · **调外部 API 的活（打标、问答）不进锁** ✓ → 它们天然可以并行 ✓
# 效果：本地方案自动变成"一件做完再做下一件"✓；纯 API 的路径不受影响 ✓
_gpu_lock = threading.Lock()
_gpu_holder = ""                 # 当前在用的是谁（写日志、界面显示用）
_gpu_waiting = 0                 # 有几个在排队


def gpu_status() -> dict:
    """给界面/接口用：显存现在被谁占着、有几个在排队 ✓"""
    return {"holder": _gpu_holder, "waiting": _gpu_waiting,
            "locked": _gpu_lock.locked()}


class gpu_slot(AbstractContextManager):
    """用显存的活**串行化** ✓（上下文管理器）。

    用法：
        with gpu_slot("OCR"):
            ... 跑 MinerU / PP-OCRv6 ...
        with gpu_slot("本地打标"):
            ... 加载本地视觉模型打标 ...

    ⚠ 调**外部 API** 的活**不要**进这个锁 ✓（它们不占本机显存，应该并行）
    """

    def __init__(self, reason: str, timeout: float = 7200.0):
        self.reason = reason
        self.timeout = timeout
        self.waited = 0.0

    def __enter__(self) -> "gpu_slot":
        global _gpu_holder, _gpu_waiting
        t0 = time.time()
        _gpu_waiting += 1
        try:
            got = _gpu_lock.acquire(timeout=self.timeout)
        finally:
            _gpu_waiting -= 1
        if not got:
            raise RuntimeError(
                f"等显存空闲超时（{self.timeout:.0f} 秒）—— 现在占着的是「{_gpu_holder}」")
        self.waited = time.time() - t0
        if self.waited > 3:
            log.info("等显存空闲用了 %.0f 秒（上一件还在跑：%s）→ 现在开始「%s」",
                     self.waited, _gpu_holder, self.reason)
        _gpu_holder = self.reason
        return self

    def __exit__(self, *exc) -> bool:
        global _gpu_holder
        _gpu_holder = ""
        _gpu_lock.release()
        return False

# ---- 各引擎的环境与桥（都在项目外，互不干扰）----
ENGINES: dict[str, dict] = {
    "ppocr": {
        "python": _p("EG2_OCR_PPOCR_PY", "ppocr_python",
                     r"D:\GJ\PP-OCRv6\.venv\Scripts\python.exe"),
        "bridge": _p("EG2_OCR_PPOCR_BRIDGE", "ppocr_bridge",
                     r"D:\GJ\PP-OCRv6\_lingxi_bridge.py"),
        "args": ["--device", "gpu:0"],
        "label": "PP-OCRv6（PaddleOCR，快、带坐标）",
        "gpu": True,
    },
    "ppocr_cpu": {
        "python": _p("EG2_OCR_PPOCR_PY", "ppocr_python",
                     r"D:\GJ\PP-OCRv6\.venv\Scripts\python.exe"),
        "bridge": _p("EG2_OCR_PPOCR_BRIDGE", "ppocr_bridge",
                     r"D:\GJ\PP-OCRv6\_lingxi_bridge.py"),
        "args": ["--device", "cpu"],
        "label": "PP-OCRv6（CPU 模式，慢但不占显存）",
        "gpu": False,
    },
    "rapid": {
        "python": _p("EG2_OCR_RAPID_PY", "rapid_python",
                     r"D:\GJ\lingxi-ocr-rapid\.venv\Scripts\python.exe"),
        "bridge": _p("EG2_OCR_RAPID_BRIDGE", "rapid_bridge",
                     r"D:\GJ\lingxi-ocr-rapid\bridge.py"),
        "args": [],
        "label": "RapidOCR（最轻，纯 CPU）",
        "gpu": False,
    },
    "mineru": {
        "python": _p("EG2_OCR_MINERU_PY", "mineru_python",
                     r"D:\GJ\MinerU\runtime\python\python.exe"),
        "bridge": _p("EG2_OCR_MINERU_BRIDGE", "mineru_bridge",
                     r"D:\GJ\MinerU\tools\submit_mineru_task.py"),
        "args": ["--backend", "pipeline", "--api-url", "http://127.0.0.1:8000"],
        "label": "MinerU（结构化 Markdown + 自动提图，最强）",
        "gpu": True,
    },
}


def list_engines() -> list[dict]:
    """给界面用：哪些引擎现在可用 ✓"""
    out = []
    for name, spec in ENGINES.items():
        ok = Path(spec["python"]).is_file() and Path(spec["bridge"]).is_file()
        item = {"id": name, "label": spec["label"], "available": ok, "gpu": spec["gpu"]}
        if name == "mineru":
            item["note"] = "需要 MinerU 的 API 在运行（run-api.cmd）"
            item["api_up"] = mineru_api_alive()
        out.append(item)
    return out


def mineru_api_alive(timeout: float = 3.0) -> bool:
    """MinerU 的 API（127.0.0.1:8000）在跑吗。"""
    import urllib.error
    import urllib.request
    for path in ("/health", "/docs"):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000" + path,
                                        timeout=timeout) as r:
                if r.status == 200:
                    return True
        except Exception:                                        # noqa: BLE001
            continue
    return False


def start_mineru_api(wait_seconds: int = 120) -> tuple[bool, str]:
    """按需启动 MinerU 的 API（它的笔记里写着要先起 run-api.cmd）✓"""
    if mineru_api_alive():
        return True, "已经在跑"
    root = _p("EG2_OCR_MINERU_DIR", "mineru_dir", r"D:\GJ\MinerU")
    cmd_file = root / "run-api.cmd"
    if not cmd_file.is_file():
        return False, f"找不到 {cmd_file}"
    try:
        proc = subprocess.Popen(["cmd", "/c", "start", "/min", "run-api.cmd"],
                         cwd=str(root), shell=False,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:                                     # noqa: BLE001
        return False, f"启动失败：{type(exc).__name__}: {exc}"
    # 记下 PID（最稳的停止依据）；cmd /c start 起的可能是新窗口，
    # 拿不到真正的 PID 也没关系，停止时会按端口再找一次 ✓
    global _mineru_started_pid
    _mineru_started_pid = None
    t0 = time.time()
    while time.time() - t0 < wait_seconds:
        time.sleep(5)
        if mineru_api_alive():
            return True, f"已就绪（等了 {time.time() - t0:.0f} 秒）"
    return False, f"等了 {wait_seconds} 秒还没起来"


def run_engine(engine: str, images: list[Path],
               timeout: float = 3600) -> dict:
    """跑一个引擎，返回统一结构。

    {"ok":bool, "engine":str, "pages":[{file,text,lines}], "error":str, "elapsed":float}
    """
    spec = ENGINES.get(engine)
    if not spec:
        return {"ok": False, "engine": engine, "error": f"未知引擎：{engine}",
                "pages": []}
    exe, bridge = Path(spec["python"]), Path(spec["bridge"])
    if not exe.is_file():
        return {"ok": False, "engine": engine, "error": f"环境不存在：{exe}",
                "pages": []}
    if not bridge.is_file():
        return {"ok": False, "engine": engine, "error": f"桥脚本不存在：{bridge}",
                "pages": []}
    if engine == "mineru" and not mineru_api_alive():
        return {"ok": False, "engine": engine,
                "error": "MinerU 的 API 没在跑（先运行 D:\\GJ\\MinerU\\run-api.cmd）",
                "pages": []}

    files = [str(p) for p in images]
    cmd = [str(exe), str(bridge)] + files + list(spec.get("args") or [])
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, cwd=str(bridge.parent))
    except subprocess.TimeoutExpired:
        return {"ok": False, "engine": engine, "error": f"超时（>{timeout:.0f} 秒）",
                "pages": [], "elapsed": time.time() - t0}
    except Exception as exc:                                     # noqa: BLE001
        return {"ok": False, "engine": engine,
                "error": f"调用失败：{type(exc).__name__}: {exc}", "pages": []}

    lines = (proc.stdout or "").strip().splitlines()
    data = None
    for ln in reversed(lines):                # 桥的最后一行才是 JSON ✓
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            try:
                data = json.loads(ln)
                break
            except ValueError:
                continue
    if data is None:
        # MinerU 的桥不是输出 JSON（它是官方的提交脚本）→ 单独处理
        return {"ok": proc.returncode == 0, "engine": engine,
                "pages": [], "elapsed": time.time() - t0,
                "raw_tail": (proc.stdout or "")[-400:],
                "error": "" if proc.returncode == 0 else
                         (proc.stderr or "")[-300:]}
    data.setdefault("elapsed", round(time.time() - t0, 2))
    return data


def ocr_images(images: list[Path], engine: str = "ppocr",
               timeout: float = 3600) -> str:
    """把若干张图 OCR 成**一段文字**（给索引器用：扫描页 → 文字）✓"""
    data = run_engine(engine, images, timeout=timeout)
    if not data.get("ok"):
        log.warning("OCR（%s）失败：%s", engine, str(data.get("error"))[:200])
        return ""
    parts: list[str] = []
    for pg in data.get("pages") or []:
        t = (pg.get("text") or "").strip()
        if t:
            parts.append(f"[{Path(pg.get('file', '')).stem}]\n{t}")
    return "\n\n".join(parts)


_mineru_started_pid: int | None = None      # 我们自己启动的 API 进程（记下来最稳）


def _proc_cmdline(pid: int) -> str:
    """取某个进程的命令行（用来核对"这个 PID 到底是不是 MinerU"）。

    ⚠ 不用 wmic —— Windows 11 默认已移除它（实测 WinError 2）✗
      改用 PowerShell 的 Get-CimInstance ✓
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30).stdout
        return (out or "").strip()
    except Exception:                                            # noqa: BLE001
        return ""


def mineru_api_pid(port: int = 8000) -> int | None:
    """找监听 8000 端口的进程 PID（= MinerU 的 API）✓"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60).stdout
    except Exception:                                            # noqa: BLE001
        return None
    for ln in (out or "").splitlines():
        if f":{port}" in ln and "LISTENING" in ln.upper():
            parts = ln.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return None


def stop_mineru_api(wait_seconds: int = 40) -> tuple[bool, str]:
    """**彻底停掉 MinerU 的 API，把显存完全还回来** ✓

    项目主 2026-10-19："夜间模式跑完 OCR，你还是应该完全停止 MinerU 的显存占用" ✓
    —— 它的 1.2B 模型会一直挂在显卡上（实测跑完还剩约 1 GB 不还 ✗），
       而且它的 API 没有"卸载模型"的接口（只有 file_parse/tasks/health）✗
       → 只能停进程 ✓

    ⚠⚠ 安全铁律：**只按 PID 杀，而且先核对命令行里确实有 mineru** ✓
       绝不能 taskkill /IM python.exe ✗ —— 这台机器上同时跑着：
         · 本项目服务（app/server.py）
         · PP-OCRv6 / RapidOCR 的桥（可能正在 OCR）
         · MinerU 的 API
       按名字杀会把它们一起杀掉 ✗
    """
    global _mineru_started_pid
    pid = _mineru_started_pid or mineru_api_pid()
    if not pid:
        return True, "MinerU API 没在跑（显存本来就没被它占）"
    cmd = _proc_cmdline(pid)
    # 核对：命令行里必须有 mineru / fast_api 之类的特征，否则拒绝动手 ✓
    low = cmd.lower()
    if cmd and not any(k in low for k in ("mineru", "fast_api", "mineru.cli")):
        return False, (f"拒绝停止：PID {pid} 的命令行看起来不是 MinerU"
                       f"（{cmd[:120]}）—— 宁可不动也不误杀 ✗")
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    except Exception as exc:                                     # noqa: BLE001
        return False, f"停止失败：{type(exc).__name__}: {exc}"
    # 等端口释放 + 显存回落 ✓
    t0 = time.time()
    while time.time() - t0 < wait_seconds:
        time.sleep(2)
        if mineru_api_pid() is None:
            _mineru_started_pid = None
            return True, (f"已停止（PID {pid}），显存已释放；"
                          f"{free_gpu_hint()}")
    return False, f"已经 kill 了 PID {pid}，但端口还没释放（可能残留子进程）"


def render_pdf_pages(pdf: Path, dpi: int = 150, first: int = 1,
                     last: int | None = None, out_dir: Path | None = None,
                     stem: str = "p") -> list[Path]:
    """把 PDF 的若干页渲染成 PNG（页号从 1 开始 ✓）。

    文件名用 `p0001.png` 这种**纯英文数字** —— 桥那边的 cv2 对中文路径
    有历史坑（虽然我们桥里已经修了，但少一层风险总是好的）✓
    """
    import tempfile
    import fitz
    out_dir = out_dir or Path(tempfile.mkdtemp(prefix="eg2ocr_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    doc = fitz.open(str(pdf))
    try:
        total = len(doc)
        end = min(last or total, total)
        for i in range(first - 1, end):
            png = out_dir / f"{stem}{i + 1:04d}.png"
            if not png.is_file():
                pix = doc[i].get_pixmap(dpi=dpi, alpha=False)
                pix.save(str(png))
            files.append(png)
    finally:
        doc.close()
    return files


def ocr_pdf(pdf: Path, engine: str = "", max_pages: int = 0,
            dpi: int = 150, batch: int = 0, work_dir: Path | None = None,
            cleanup: bool = True, start_page: int = 1,
            end_page: int = 0) -> dict:
    """把 PDF 渲染成页图再 OCR，返回文字 + **页数限制的明确提示** ✓。

    项目主 2026-10-19："页面限制太死板，默认 100 以内，应该还要有个提示和
    调节选项，如有超过页码限制能提醒我与我能手动修改" ✓

    `start_page`（同日新增，项目主的追问）：
      "调高页码数后，原已 OCR 的页面不会重新 OCR 吧"
      → 传 start_page=N+1 就**从第 N+1 页接着做**，前面的不重做 ✓
      （页数记录存在 files.ocr_pages，由索引器负责传进来 ✓）

    返回：
      {"ok", "text", "pages_done", "total_pages", "truncated", "notice",
       "engine", "elapsed", "page_from", "page_to"}
      · truncated=True 表示**超出上限、只做了前 N 页** ✓
      · page_from/page_to = 这次实际做的页码范围 ✓（调用方据此更新"已做到第几页"）
    """
    import shutil as _sh
    import tempfile

    result = {"ok": False, "text": "", "pages_done": 0, "total_pages": 0,
              "truncated": False, "notice": "", "engine": engine, "elapsed": 0.0,
              "page_from": 0, "page_to": 0}
    t0 = time.time()
    try:
        import fitz
    except ImportError:
        result["notice"] = "没装 PyMuPDF，无法渲染 PDF 页"
        return result

    try:
        doc = fitz.open(str(pdf))
        total = len(doc)
        doc.close()
    except Exception as exc:                                     # noqa: BLE001
        result["notice"] = f"PDF 打不开：{type(exc).__name__}: {exc}"
        return result

    result["total_pages"] = total
    # ★ 0 = 不限，**按实际页数全部做**（2026-10-19 项目主要求的默认）✓
    #   想只做一部分就填具体数字（比如 50）✓
    raw_limit = int(max_pages or 0)
    limit = raw_limit if raw_limit > 0 else total
    # 上次做到第几页 → 这次从下一页开始（不重做）✓
    # ⚠ 这里**不能** min(total) —— 若 start_page 已超过总页数，
    #   说明全做完了，应该走下面"没有新页"的早退分支，而不是硬做最后一页 ✗
    first = max(1, int(start_page or 1))
    # ★ 页码范围（2026-10-19 项目主："给个 pdf 文件选择页码范围操作选项"）✓
    #   end_page = 0 表示"到最后一页"；给了就只做到那一页 ✓
    last_want = int(end_page or 0)
    if last_want <= 0 or last_want > total:
        last_want = total
    # 三个约束取最小：总页数、页码范围末端、页数上限 ✓
    do_from, do_to = first, min(total, last_want, limit)
    result["page_from"], result["page_to"] = do_from, max(0, do_to)
    result["truncated"] = total > limit
    if do_to < first:
        # 上限没变高（或已经做完了）→ 没有新页可做 ✓
        result["ok"] = True
        result["notice"] = (f"没有新页需要 OCR（已做到第 {first - 1} 页，"
                            f"上限 {limit} 页，共 {total} 页）")
        result["elapsed"] = round(time.time() - t0, 1)
        return result
    if result["truncated"]:
        # ★ 明确提示（会写进备注，界面上能找到）✓
        result["notice"] = (
            f"扫描版 OCR：这个 PDF 共 {total} 页，超过页数上限 {limit} 页 —— "
            f"这次处理第 {do_from}~{do_to} 页（还有 {total - do_to} 页没做）。"
            f"要全做：在「设置 → OCR」里把「页数上限」调大，"
            f"或在 config.ini 的 search.ocr_max_pages 改；"
            f"调大后重扫会**接着做没做过的页**，不会重做已完成的 ✓")
        log.warning("PDF 页数超限：%s 共 %d 页 > 上限 %d 页", pdf.name, total, limit)
    else:
        result["notice"] = ""

    if not engine:
        engine = pick_engine(has_text=False)
    if not engine:
        result["notice"] = (result["notice"] + " 没有可用的 OCR 引擎（检查 "
                            "D:\\GJ\\PP-OCRv6 与 D:\\GJ\\lingxi-ocr-rapid）").strip()
        return result

    tmp = work_dir or Path(tempfile.mkdtemp(prefix="eg2ocr_"))
    parts: list[str] = []
    done = 0
    # ★ 用显存的活要串行（项目主：OCR 与打标同时跑会撞显存）✓
    #   注意：engine 可能是 CPU 的（rapid / ppocr_cpu）→ 那种不必占锁 ✓
    use_gpu = bool((ENGINES.get(engine) or {}).get("gpu"))
    slot = gpu_slot(f"OCR({engine})") if use_gpu else None
    try:
        if slot is not None:
            slot.__enter__()
        # ★ 批量大小按**可用显存**自适应（项目主："根据本机 8G 显卡限制进程数"）✓
        #   显存紧就几张几张地做，一口气塞 20 张会爆 ✗
        step = int(batch) if int(batch) > 0 else (auto_batch() if use_gpu else 8)
        step = max(1, step)
        log.info("OCR 批量 = %d 页/批（%s）", step,
                 free_gpu_hint() or "显存未知")
        # 分批渲染 + 分批识别：一次塞 300 页会吃爆内存 ✗
        # ★ 从 do_from 开始（上次做到哪就从下一页接着做，不重做 ✓）
        for start in range(do_from, do_to + 1, step):
            end = min(start + step - 1, do_to)
            pages = render_pdf_pages(pdf, dpi=dpi, first=start, last=end,
                                     out_dir=tmp)
            data = run_engine(engine, pages)
            if not data.get("ok"):
                result["notice"] = (result["notice"] + " " +
                                    f"OCR 失败：{str(data.get('error'))[:160]}").strip()
                break
            got = 0
            for pg in data.get("pages") or []:
                t = (pg.get("text") or "").strip()
                if t:
                    # 带上页码 —— 问答引用时要能说"第 N 页" ✓
                    num = "".join(ch for ch in Path(pg.get("file", "")).stem
                                  if ch.isdigit()).lstrip("0") or str(start + got)
                    parts.append(f"[第 {num} 页]\n{t}")
                    got += 1
            done += len(pages)
            log.info("OCR 进度：%s 第 %d~%d 页（本次已 %d 页，引擎 %s）",
                     pdf.name, start, end, done, engine)
    finally:
        if slot is not None:
            slot.__exit__(None, None, None)
        if cleanup:
            _sh.rmtree(tmp, ignore_errors=True)

    result["ok"] = bool(parts)
    result["text"] = "\n\n".join(parts)
    result["pages_done"] = done
    result["engine"] = engine
    result["elapsed"] = round(time.time() - t0, 1)
    if not result["ok"] and not result["notice"]:
        result["notice"] = f"OCR 没识别出文字（引擎 {engine}，试了 {done} 页）"
    return result


# 每页少于这么多字 → 认为"没提取到有效正文"，该走 OCR ✓
# ⚠ 2026-10-19 实测踩到：地藏经 160 页，**有 515 字**文字层（每页 3.2 字），
#   按"文字非空就算文字版"的老判据 → 它会跳过 OCR，正文全丢 ✗
#   改成看**密度**才对 ✓
MIN_CHARS_PER_PAGE = 50


def auto_batch(default: int = 20) -> int:
    """按**可用显存**决定一次送几张页图去 OCR ✓

    项目主 2026-10-19："根据本机 8G 显卡限制进程数保证顺利完成"
    → 8G 卡上，嵌入模型约 1.3GB，OCR 引擎约 1GB，剩 5~6GB 给批处理。
      一张 150DPI 的 A4 扫描页（1241×1754）在识别时约占 500~800MB 峰值，
      所以按 `free_mb // 800` 算，再夹在 1~default 之间 ✓
    → 显存紧就自动降到 1~3 张，不会一口气塞爆 ✓
    → 拿不到显存信息时给一半（保守）✓
    """
    try:
        import torch
        if torch.cuda.is_available():
            free, _total = torch.cuda.mem_get_info()
            free_mb = free / 1024 / 1024
            n = int(free_mb // 800)
            return max(1, min(int(default), n))
    except Exception:                                            # noqa: BLE001
        pass
    return max(1, int(default) // 2)


def pdf_text_density(path: Path) -> dict:
    """这个 PDF 的文字密度（判断该不该 OCR 的依据）✓

    返回 {"chars": 总字数, "pages": 页数, "per_page": 每页字数, "needs_ocr": bool}
    """
    out = {"chars": 0, "pages": 0, "per_page": 0.0, "needs_ocr": True}
    try:
        import fitz
        doc = fitz.open(str(path))
        try:
            pages = len(doc)
            chars = 0
            for i in range(pages):
                try:
                    chars += len(doc[i].get_text() or "")
                except Exception:                                # noqa: BLE001
                    continue
        finally:
            doc.close()
    except Exception as exc:                                     # noqa: BLE001
        log.debug("读 PDF 文字密度失败（%s）：%s", path, exc)
        return out
    per = (chars / pages) if pages else 0.0
    out.update(chars=chars, pages=pages, per_page=round(per, 1),
               needs_ocr=(pages > 0 and per < MIN_CHARS_PER_PAGE))
    return out


def pick_engine(prefer: str = "", *, has_text: bool = True) -> str:
    """按配置和实际情况挑一个引擎。

    · 有文字层 → 优先 fitz（不花钱不出错）；要插图/表格则 mineru
    · 没文字层（扫描版）→ 优先 ppocr（快），mineru 次之，rapid 兜底
    """
    prefer = (prefer or "").strip().lower()
    if prefer in ENGINES and _usable(prefer):
        return prefer
    order = (["mineru", "ppocr", "rapid"] if not has_text
             else ["ppocr", "rapid"])
    for name in order:
        if _usable(name):
            return name
    return ""


def _usable(name: str) -> bool:
    spec = ENGINES.get(name) or {}
    if not (Path(spec.get("python", "")).is_file()
            and Path(spec.get("bridge", "")).is_file()):
        return False
    if name == "mineru" and not mineru_api_alive():
        return False
    return True


def free_gpu_hint() -> str:
    """给日志用：当前 CUDA 可见显存（拿不到就返回空）✓"""
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            return f"显存空闲 {free / 1024 / 1024:.0f} / {total / 1024 / 1024:.0f} MB"
    except Exception:                                            # noqa: BLE001
        pass
    return ""


__all__ = ["ENGINES", "list_engines", "run_engine", "ocr_images", "ocr_pdf",
           "render_pdf_pages", "pick_engine", "mineru_api_alive",
           "mineru_api_pid", "start_mineru_api", "stop_mineru_api",
           "free_gpu_hint", "gpu_slot", "gpu_status"]
