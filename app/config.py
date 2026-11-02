"""配置读取。

只读取本文件夹内的 config.ini，不读写系统环境变量、不改注册表。
"""
from __future__ import annotations

import configparser
import os
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.ini"

_MODALITY_KEYS = {
    # modality -> (vision_config, audio_config)
    "full": (True, True),
    "text_image": (True, False),
    "text_audio": (False, True),
    "text": (False, False),
}


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "yes", "true", "on", "y")


def patch_ini(path, section: str, values: dict) -> list[str]:
    """**就地改 config.ini 的若干项，保留全部注释和顺序** ✓

    为什么不直接 configparser.write()：那会把注释和排版全丢掉 ✗
    （项目主很在意 config.ini 里的中文说明 ✓）

    做法：逐行扫，命中 `[section]` 之后出现的 `key = xxx` 就只换值；
    找不到的 key 插到该段末尾 ✓。返回实际改动的项名。
    """
    path = Path(path)
    # ★ 保留原文件的换行风格（2026-10-19 实测：原文是 CRLF，我写 LF 会变成混合换行 ✗
    #   虽然读起来没问题，但项目主会打开这个文件看/改，混着很难受 ✓）
    raw = path.read_bytes()
    eol = "\r\n" if b"\r\n" in raw else "\n"
    lines = raw.decode("utf-8").split(eol)
    changed: list[str] = []
    sec = ""
    sec_end = -1
    seen: dict[str, int] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if sec == section and sec_end < 0:
                sec_end = i                    # 这一段到此结束（供插入用）
            sec = s[1:-1].strip()
            continue
        if sec != section:
            continue
        if not s or s.startswith(";") or s.startswith("#"):
            continue
        if "=" in s:
            key = s.split("=", 1)[0].strip()
            seen[key] = i
            if key in values:
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = f"{indent}{key} = {values[key]}"
                changed.append(key)
    missing = [k for k in values if k not in seen]
    if missing:
        if sec_end < 0:
            sec_end = len(lines)
        lines[sec_end:sec_end] = [f"{k} = {values[k]}" for k in missing]
        changed.extend(missing)
    path.write_bytes(eol.join(lines).encode("utf-8"))
    return changed


class Config:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        self._p = configparser.ConfigParser()
        self.reload()

    # ------------------------------------------------------------ 读取/回写
    def reload(self) -> None:
        """重新读取 config.ini（界面改完设置后调用，无需重启进程）。"""
        parser = configparser.ConfigParser()
        if self.path.exists():
            parser.read(self.path, encoding="utf-8")
        self._p = parser

    def raw_text(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def update(self, values: dict[str, str]) -> list[str]:
        """按 "section.option" 就地更新 config.ini，保留原有注释与顺序。

        只改动目标那一行，不会像 configparser.write() 那样把注释全部抹掉。
        """
        text = self.raw_text()
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(newline) if text else []

        section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
        option_re = re.compile(r"^(\s*)([A-Za-z0-9_.\-]+)(\s*)=.*$")

        def spans() -> dict[str, list[int]]:
            found: dict[str, list[int]] = {}
            order: list[tuple[int, str]] = []
            for i, line in enumerate(lines):
                m = section_re.match(line)
                if m:
                    name = m.group(1).strip().lower()
                    found[name] = [i, len(lines)]
                    order.append((i, name))
            for idx, (start, name) in enumerate(order):
                found[name][1] = order[idx + 1][0] if idx + 1 < len(order) else len(lines)
            return found

        table = spans()
        changed: list[str] = []

        for key, value in values.items():
            section, _, option = key.partition(".")
            section = section.strip().lower()
            option = option.strip()
            if not section or not option:
                continue
            value = str(value)

            if section not in table:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(f"[{section}]")
                lines.append(f"{option} = {value}")
                table = spans()
                changed.append(key)
                continue

            begin, end = table[section]
            replaced = False
            for i in range(begin + 1, end):
                m = option_re.match(lines[i])
                if m and m.group(2).lower() == option.lower():
                    lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}= {value}"
                    replaced = True
                    break
            if not replaced:
                insert_at = end
                while insert_at > begin + 1 and lines[insert_at - 1].strip() == "":
                    insert_at -= 1
                lines.insert(insert_at, f"{option} = {value}")
                table = spans()
            changed.append(key)

        self.path.write_text(newline.join(lines), encoding="utf-8")
        self.reload()
        return changed

    def _get(self, section: str, option: str, default: str = "") -> str:
        try:
            return self._p.get(section, option)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    # ---------- server ----------
    @property
    def host(self) -> str:
        return os.environ.get("EG2_HOST") or self._get("server", "host", "127.0.0.1").strip()

    @property
    def port(self) -> int:
        env = os.environ.get("EG2_PORT")
        if env:
            try:
                return int(env)
            except ValueError:
                pass
        return int(self._get("server", "port", "8199").strip() or 8199)

    @property
    def open_browser(self) -> bool:
        return _as_bool(self._get("server", "open_browser", "yes"), True)

    # ---------- model ----------
    @property
    def repo_id(self) -> str:
        return self._get("model", "repo_id", "google/embeddinggemma-2").strip()

    @property
    def model_dir(self) -> Path:
        raw = self._get("model", "model_dir", "models/embeddinggemma-2").strip()
        p = Path(raw)
        return p if p.is_absolute() else (ROOT_DIR / p)

    @property
    def device(self) -> str:
        return self._get("model", "device", "auto").strip().lower() or "auto"

    @property
    def dtype(self) -> str:
        return self._get("model", "dtype", "auto").strip().lower() or "auto"

    @property
    def modality(self) -> str:
        value = self._get("model", "modality", "full").strip().lower() or "full"
        return value if value in _MODALITY_KEYS else "full"

    @property
    def use_vision(self) -> bool:
        return _MODALITY_KEYS[self.modality][0]

    @property
    def use_audio(self) -> bool:
        return _MODALITY_KEYS[self.modality][1]

    @property
    def default_dim(self) -> int:
        try:
            return int(self._get("model", "default_dim", "768").strip() or 768)
        except ValueError:
            return 768

    @property
    def batch_size(self) -> int:
        try:
            return max(1, int(self._get("model", "batch_size", "8").strip() or 8))
        except ValueError:
            return 8

    @property
    def max_length(self) -> int:
        try:
            return int(self._get("model", "max_length", "8192").strip() or 8192)
        except ValueError:
            return 8192

    @property
    def offline(self) -> bool:
        return _as_bool(self._get("model", "offline", "yes"), True)

    # ---------- encoding ----------
    @property
    def default_prompt(self) -> str:
        return self._get("encoding", "default_prompt", "none").strip() or "none"

    @property
    def normalize(self) -> bool:
        return _as_bool(self._get("encoding", "normalize", "yes"), True)

    # ---------- download ----------
    @property
    def mirror(self) -> str:
        return self._get("download", "mirror", "").strip()

    @property
    def proxy(self) -> str:
        return self._get("download", "proxy", "").strip()

    @property
    def source(self) -> str:
        value = self._get("download", "source", "auto").strip().lower() or "auto"
        return value if value in ("auto", "huggingface", "modelscope") else "auto"

    @property
    def workers(self) -> int:
        try:
            return max(1, int(self._get("download", "workers", "4").strip() or 4))
        except ValueError:
            return 4

    # ---------- search ----------
    def _int(self, option: str, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(self._get("search", option, str(default)).strip() or default))
        except ValueError:
            return default

    def _list(self, option: str, default: str = "") -> list[str]:
        raw = self._get("search", option, default)
        return [item.strip().lower() for item in raw.split(",") if item.strip()]

    @property
    def index_dim(self) -> int:
        value = self._int("index_dim", 256, 1)
        if value not in (128, 256, 512, 768):
            value = min((d for d in (128, 256, 512, 768) if d >= value), default=768)
        return value

    @property
    def allow_register(self) -> bool:
        """允不允许自助注册（默认**不允许**，公网上不该随便开门）。

        项目主 2026-10-21 外网部署：注册进来的账号是 `pending`，
        还得管理员审批；但连注册都不该默认开放。
        要开就在 config.ini 里写：
            [multi]
            allow_register = yes
        """
        raw = str(self._get("multi", "allow_register", "no")).strip().lower()
        return raw in ("yes", "true", "1", "on", "y")

    @property
    def local_strict(self) -> bool:
        """本机是否也**按归属隔离**数据（B 方案，默认 yes）✓

        · yes（默认）：本机的普通用户也只能看自己名下 + 共享的，
                      看不到管理员的私有文件夹 —— 和别人用同一台电脑时安全 ✓
        · no         ：本机 = 个人专用，任何登录的人都看到全部（A 方案）

        项目主 2026-10-21："给个开关吧，希望是B方案"
        ⚠ 只影响**数据可见性/可写性**；建库、索引、打标这些"本机操作能力"
          仍然对普通用户开放（那是他自己的机器）✓
        """
        raw = str(self._get("multi", "local_strict", "yes")).strip().lower()
        return raw not in ("no", "false", "0", "off", "否")

    @property
    def chunk_chars(self) -> int:
        return self._int("chunk_chars", 800, 100)

    @property
    def chunk_overlap(self) -> int:
        return min(self._int("chunk_overlap", 120), self.chunk_chars // 2)

    @property
    def max_chunks_per_file(self) -> int:
        return self._int("max_chunks_per_file", 40, 1)

    @property
    def max_file_mb(self) -> int:
        return self._int("max_file_mb", 20, 1)

    # ---------------- OCR / 扫描版（2026-10-19 新增）----------------
    # 项目主的要求：默认 100 页以内；**超过要有提示**，而且**能自己调** ✓
    # 所以这三个都写进 config.ini（手改也行），界面上也能改 ✓
    @property
    def ocr_max_pages(self) -> int:
        """一个 PDF 最多 OCR 多少页。**0 = 全部做**（2026-10-19 项目主要求的默认）。

        项目主的原话："干脆做成默认自动方式，按实际页码全部 OCR，
        当然需根据本机 8G 显卡限制进程数保证顺利完成即可，
        手动选项也保留住（有时可能只需 ocr 一部分内容）"

        所以：
          · 默认 **0**（= 按实际页数全部做）✓
          · 想只做一部分：填具体数字（比如 50 → 只做前 50 页）✓
          · 无论哪种，**已做过的页不会重做**（files.ocr_pages 记着）✓
          · 节奏由**显存自适应**控制（见 ocr.auto_batch）✓
        """
        return self._int("ocr_max_pages", 0, 0)

    @property
    def ocr_max_images(self) -> int:
        """一个 PDF 最多给多少张插图打标。**0 = 全部**（2026-10-19 项目主要求）。

        项目主："插图打标也有限制吗？也做成默认 PDF 插图自动全部打标，手动也保留"
        → 默认 **0**：筛出来的真插图**全部**打标 ✓
        → 想省时间就填具体数字（比如 30）✓
        （筛选规则在 app/pdfimages.py：去掉图标/装饰/重复 ✓）
        """
        return self._int("ocr_max_images", 0, 0)

    @property
    def ocr_page_from(self) -> int:
        """OCR 起始页（默认 1）✓
        项目主："给个 pdf 文件选择页码范围操作选项，要不只能从第一页开始算"
        """
        return self._int("ocr_page_from", 1, 1)

    @property
    def ocr_page_to(self) -> int:
        """OCR 结束页（默认 0 = 到最后一页）✓"""
        return self._int("ocr_page_to", 0, 0)

    @property
    def ocr_engine(self) -> str:
        """OCR 引擎：auto / mineru / ppocr / ppocr_cpu / rapid。

        auto = 有文字层就不 OCR；扫描版优先 mineru，它不可用就 ppocr ✓
        """
        v = (self._get("search", "ocr_engine", "auto") or "auto").strip().lower()
        return v if v in ("auto", "mineru", "ppocr", "ppocr_cpu", "rapid") else "auto"

    @property
    def ocr_enabled(self) -> bool:
        """全局 OCR 总开关（每个文件夹还能各自覆盖 ✓）。

        默认关 —— 它慢且吃显存，不该在所有文件夹上默认打开 ✗
        """
        return _as_bool(self._get("search", "ocr_enabled", "no"), False)

    @property
    def search_batch_size(self) -> int:
        return self._int("batch_size", 16, 1)

    @property
    def index_images(self) -> bool:
        return _as_bool(self._get("search", "index_images", "yes"), True)

    @property
    def image_max_side(self) -> int:
        """送进视觉编码器前，图片的最长边上限（只在内存里缩）。

        模型每张图固定编码成 280 个 token（约 816px），所以给 1024 就够；
        调大更清晰但更慢，调小更快但细节少。
        """
        try:
            v = int(float(self._get("model", "image_max_side", "1024")))
        except ValueError:
            v = 1024
        return min(max(v, 224), 4096)

    # ---------------- 本地对话模型（可选，做知识库问答用）----------------
    @property
    def chat_model_dir(self) -> Path:
        return Path(self._get("chat", "model_dir", "models/Qwen2.5-1.5B-Instruct"))

    @property
    def chat_device(self) -> str:
        v = self._get("chat", "device", "auto").strip().lower()
        return v if v in ("auto", "cuda", "cpu") else "auto"

    @property
    def chat_max_tokens(self) -> int:
        try:
            v = int(float(self._get("chat", "max_new_tokens", "600")))
        except ValueError:
            v = 600
        return min(max(v, 64), 2048)

    @property
    def chat_temperature(self) -> float:
        try:
            v = float(self._get("chat", "temperature", "0.2"))
        except ValueError:
            v = 0.2
        return min(max(v, 0.0), 1.5)

    @property
    def chat_top_k(self) -> int:
        try:
            v = int(float(self._get("chat", "top_k", "5")))
        except ValueError:
            v = 5
        return min(max(v, 1), 20)

    # ---------------- 人脸（可选功能）----------------
    # 依赖和权重放在 face-lab\ 独立目录，详见 app/faces.py
    @property
    def face_enabled(self) -> bool:
        return _as_bool(self._get("face", "enabled", "no"), False)

    def _face_float(self, key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(self._get("face", key, str(default)))
        except ValueError:
            v = default
        return min(max(v, lo), hi)

    def _face_int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(float(self._get("face", key, str(default))))
        except ValueError:
            v = default
        return min(max(v, lo), hi)

    @property
    def face_min_prob(self) -> float:
        return self._face_float("min_prob", 0.90, 0.30, 0.999)

    @property
    def face_min_size(self) -> int:
        return self._face_int("min_size", 60, 20, 400)

    @property
    def face_assign_threshold(self) -> float:
        """归到某个已知人物的相似度门槛（实测同人约 0.88，异人约 0.32）。"""
        return self._face_float("assign_threshold", 0.55, 0.20, 0.95)

    @property
    def face_search_threshold(self) -> float:
        """以图搜人的召回门槛，比归类宽松些。"""
        return self._face_float("search_threshold", 0.45, 0.10, 0.95)

    @property
    def face_cluster_threshold(self) -> float:
        """自动聚类成「人物」的阈值（average-linkage 实测 0.65 最平衡）。"""
        return self._face_float("cluster_threshold", 0.65, 0.30, 0.95)

    @property
    def index_audio(self) -> bool:
        return _as_bool(self._get("search", "index_audio", "no"), False)

    @property
    def index_video(self) -> bool:
        return _as_bool(self._get("search", "index_video", "no"), False)

    @property
    def auto_scan_on_start(self) -> bool:
        return _as_bool(self._get("search", "auto_scan_on_start", "yes"), True)

    @property
    def ignore_dirs(self) -> set[str]:
        return set(self._list("ignore_dirs", "node_modules,__pycache__,.git,venv,.venv,.svn,dist,build,out,target,.idea,.vs,.cache,$RECYCLE.BIN,System Volume Information,.syncing_db,@Recently-Snapshot,@Recycle,#recycle,@eaDir,.snapshot,#snapshot,.@__thumb,@tmp,.thumbnails"))

    @property
    def skip_ext(self) -> set[str]:
        return set(self._list("skip_ext", ""))

    @property
    def index_dir(self) -> Path:
        return ROOT_DIR / "data" / "index"

    @property
    def db_path(self) -> Path:
        return self.index_dir / "index.db"

    # ---------- paths ----------
    @property
    def root(self) -> Path:
        return ROOT_DIR

    @property
    def logs_dir(self) -> Path:
        return ROOT_DIR / "logs"

    @property
    def run_dir(self) -> Path:
        return ROOT_DIR / "run"

    @property
    def data_dir(self) -> Path:
        return ROOT_DIR / "data"

    @property
    def tmp_dir(self) -> Path:
        return ROOT_DIR / "data" / "tmp"

    def ensure_dirs(self) -> None:
        for d in (self.logs_dir, self.run_dir, self.data_dir, self.tmp_dir, self.index_dir):
            d.mkdir(parents=True, exist_ok=True)
