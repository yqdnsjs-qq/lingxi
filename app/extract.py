"""从各种文件里抽取可用于检索的文本。

只依赖标准库（PDF 用可选的 pypdf）：
  * 纯文本 / 源码 / 配置：按编码猜测读取
  * docx / xlsx / pptx：本质是 zip + XML，直接用 zipfile 解析，不装额外库
  * pdf：有 pypdf 就抽正文，没有就只索引文件名
  * 图片 / 音频 / 视频：不抽文本，交给模型的对应编码器
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".ini", ".cfg", ".conf", ".properties", ".env", ".toml",
    ".xml", ".html", ".htm", ".xhtml", ".css", ".scss", ".less", ".vue", ".svelte",
    ".py", ".pyi", ".ipynb", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".java", ".kt", ".kts", ".scala", ".groovy", ".cs", ".vb", ".fs",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".m", ".mm", ".swift",
    ".go", ".rs", ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".jl", ".dart",
    ".sql", ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1", ".psm1", ".vbs",
    ".tex", ".bib", ".srt", ".vtt", ".ass", ".lrc", ".po", ".pot", ".diff", ".patch",
    ".gradle", ".cmake", ".mk", ".makefile", ".dockerfile", ".gitignore", ".editorconfig",
}
SPECIAL_NAMES = {
    "makefile", "dockerfile", "readme", "license", "licence", "changelog",
    "requirements.txt", "cmakelists.txt", "gemfile", "procfile",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".gif", ".webp",
              ".tif", ".tiff", ".heic", ".heif", ".avif", ".ico", ".pcx", ".tga"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}
PDF_EXTS = {".pdf"}
OOXML_EXTS = {".docx": "word", ".xlsx": "excel", ".pptx": "powerpoint"}

_TAG_RE = re.compile(rb"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0]+")


def classify(path: Path) -> str:
    """返回 text / pdf / docx / xlsx / pptx / image / audio / video / other。"""
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return "text"
    if path.name.lower() in SPECIAL_NAMES:
        return "text"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in OOXML_EXTS:
        return ext.lstrip(".")
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return "other"


# ------------------------------------------------------------------ 纯文本
def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "big5"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def read_text_file(path: Path, max_bytes: int) -> str:
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes)
    return _decode(raw)


# ------------------------------------------------------------------ Office
def _xml_text(raw: bytes) -> str:
    """把一段 OOXML 片段变成纯文本：去掉标签、保留段落分隔。"""
    raw = re.sub(rb"<w:p[ >]", b"\n<w:p ", raw)          # Word 段落
    raw = re.sub(rb"</a:p>", b"\n", raw)                  # PPT 段落
    raw = re.sub(rb"<[^>]*?/>", b" ", raw)
    text = _TAG_RE.sub(b" ", raw).decode("utf-8", errors="replace")
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _docx_text(path: Path, budget: int) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for name in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
            if name in zf.namelist():
                parts.append(_xml_text(zf.read(name)))
                if sum(len(p) for p in parts) > budget:
                    break
    return "\n".join(parts)


def _pptx_text(path: Path, budget: int) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        slides = sorted(n for n in zf.namelist()
                        if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        for name in slides:
            parts.append(_xml_text(zf.read(name)))
            if sum(len(p) for p in parts) > budget:
                break
    return "\n".join(parts)


def _xlsx_text(path: Path, budget: int) -> str:
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            shared = _xml_text(zf.read("xl/sharedStrings.xml")).splitlines()
        parts: list[str] = []
        sheets = sorted(n for n in zf.namelist()
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        for name in sheets:
            raw = zf.read(name)
            values: list[str] = []
            for cell in re.findall(rb"<c[^>]*?(?:t=\"s\")?[^>]*>(.*?)</c>", raw, re.S):
                text = _TAG_RE.sub(b" ", cell).decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if text.isdigit() and int(text) < len(shared):
                    text = shared[int(text)]
                values.append(text)
            if values:
                parts.append(" | ".join(values))
            if sum(len(p) for p in parts) > budget:
                break
    return "\n".join(parts)


# ------------------------------------------------------------------ PDF
_END_PUNCT = ("。", "！", "？", "：", "；", ".", "!", "?", ":", ";", "”", "」", "』")


def _join_short_lines(text: str) -> str:
    """把 PDF 提取出来的**碎行**合并成正常句子。

    ★ 2026-10-19 加（项目主：花生壳手册明明有"内网穿透"的设置步骤，模型却说
      「资料里没有提到」）。
      原因不在检索（检索排得挺好），而在**文本本身**：PDF 提取常把一句话拆成
      一个字一行 —— `花生\\n壳盒子\\n用户手册`，再加上目录里成百个点号，
      模型读这种文本基本读不懂 ✗

    规则（保守，宁可不合并也不要合错）：
      · 本行很短（≤ 22 字）
      · 上一行**不以句末标点结尾**（没说完）
      · 本行**不是**目录行（点号 ≥ 4 个的跳过）
    满足才拼到上一行后面 ✓
    """
    out: list[str] = []
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            out.append("")
            continue
        if (out and len(s) <= 22 and out[-1]
                and not out[-1].endswith(_END_PUNCT)
                and s.count(".") < 4 and s.count("…") < 2):
            out[-1] = out[-1] + s
            continue
        out.append(s)
    return "\n".join(out)


def _pdf_text(path: Path, budget: int) -> str:
    """提取 PDF 文字：**优先 PyMuPDF(fitz)，失败才回退 pypdf**。

    ★ 2026-10-19 换的顺序（这一条很关键）：
      项目主发现有 PDF 带「页面提取：不允许」的**权限标记**。
      `pypdf` **尊重**这个标记 → 52 页只能提出 129 个字 ✗
      → 库里被记成「没有可提取的文字」→ 知识库问答永远答不出 ✗
      而 `fitz` 不受这个标记限制 → 同样 31 页能提出 9403 个字 ✓（还快 15 倍）
      实测对照：花生壳手册 pypdf 129 字 / fitz 9403 字。
      所以顺序反过来：先 fitz，fitz 提不出（真扫描版）才试 pypdf ✓
    """
    try:
        import fitz
        doc = fitz.open(str(path))
        try:
            parts: list[str] = []
            for i in range(len(doc)):
                try:
                    parts.append(doc[i].get_text() or "")
                except Exception:                                # noqa: BLE001
                    continue
                if sum(len(x) for x in parts) > budget:
                    break
            text = "\n".join(parts)
            if text.strip():
                return _join_short_lines(text)          # ★ 合并碎行（PDF 提取必需）✓
        finally:
            doc.close()
    except Exception as exc:                                     # noqa: BLE001
        log.debug("fitz 提取 PDF 失败，回退 pypdf：%s", exc)

    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception:  # noqa: BLE001
        return ""
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
        if sum(len(p) for p in parts) > budget:
            break
    return "\n".join(parts)


# ------------------------------------------------------------------ 入口
def extract(path: Path, max_bytes: int) -> tuple[str, str]:
    """返回 (类型, 文本)。类型见 classify()；抽不出文本时文本为空串。"""
    kind = classify(path)
    budget = max_bytes
    try:
        if kind == "text":
            return kind, read_text_file(path, max_bytes)
        if kind == "docx":
            return kind, _docx_text(path, budget)
        if kind == "pptx":
            return kind, _pptx_text(path, budget)
        if kind == "xlsx":
            return kind, _xlsx_text(path, budget)
        if kind == "pdf":
            return kind, _pdf_text(path, budget)
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
        return kind, ""
    return kind, ""


def kind_label(kind: str) -> str:
    return {
        "text": "文本", "pdf": "PDF", "docx": "Word", "xlsx": "Excel",
        "pptx": "PPT", "image": "图片", "audio": "音频", "video": "视频",
        "other": "其它",
    }.get(kind, kind)


def supports_content(kind: str, cfg) -> bool:
    if kind in ("text", "pdf", "docx", "xlsx", "pptx"):
        return bool(getattr(cfg, "index_text", True))
    if kind == "image":
        return bool(cfg.index_images and cfg.use_vision)
    if kind == "audio":
        return bool(cfg.index_audio and cfg.use_audio)
    if kind == "video":
        return bool(cfg.index_video and cfg.use_vision)
    return False


# ============================================================ 文本清洗（默认）
# 项目主 2026-10-18：入库前先洗一遍，检索更精准 ✓
# 这个函数在**索引时默认执行**（indexer 提取完文本、切块之前）✓，不需要任何开关。
#
# 洗什么（实测遇到的问题）：
#   · 控制字符 / 零宽字符 → 嵌入模型会报
#       "TextEncodeInput must be Union[TextInputSequence, ...]" ✗
#   · 纯噪声行（微信剪藏里的 URL 编码串、`&exptype=...&fasttmpl_flag=0` 之类）
#     → 被当正文切块，检索命中它、模型只能说"资料里没提到" ✗
#   · 多余空行 / 行尾空白
# 不清什么：正文、代码、表格里的正常字符一律保留 ✓
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZW_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
# ★ 孤立代理字符（U+D800–U+DFFF）—— 2026-10-19 挖到的真凶 ✓
#   PDF 里的 emoji（如 🌟 = \ud83c\udf1f 两个代理）被提取工具拆散，
#   只剩**一半**时就成了"孤立代理"：它不是合法字符，
#   tokenizer 会直接抛 TypeError: TextEncodeInput must be Union[...] ✗
#   （实测：某片 996 字里第 389 个字符是 \ud83c，整片就编不过 ✓）
_SURR_RE = re.compile(r"[\ud800-\udfff]")
# hard_clean 要丢的"危险字符"（正常清洗已经处理一部分，这里是兜底）
_SAFE_DROP_RE = re.compile(
    "[\ud800-\udfff"                 # 代理（半个 emoji）
    "\ue000-\uf8ff"                  # 私有区
    "\ufff0-\uffff"                  # 非字符
    "\x00-\x08\x0b\x0c\x0e-\x1f\x7f"  # 控制字符
    "]")


def _fix_surrogates(text: str) -> str:
    """处理孤立代理字符：能配对的恢复成真 emoji ✓，配不上的删掉 ✓。

    Python 的 str 允许"半个代理"存在（surrogatepass），但嵌入模型的 tokenizer
    不接受 ✗ —— 所以必须在这一步清掉，否则整片文字都进不了库。
    """
    if not _SURR_RE.search(text):
        return text
    try:
        # 合法的代理对 → 重组成真正的字符（emoji 保住 ✓）
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "strict")
    except UnicodeDecodeError:
        # 有孤立的 → 把不能配对的那部分丢掉（宁可少个 emoji，也别让整片失败）✓
        fixed = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        return fixed.replace("\ufffd", "")


def _readable_ratio(s: str) -> float:
    good = 0
    for ch in s:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" or ch in " ，。、；：！？（）《》-—·.,:;!?()[]":
            good += 1
    return good / max(1, len(s))


def _is_noise_line(line: str) -> bool:
    """判断这一行是不是"该丢的噪声"（实测踩过的两类 ✓）：
      · URL 编码串：`xxx%2Fyyy%3D%3D&a=1&b=2`（微信剪藏的图片链接参数）
      · 长参数串：一堆 `k=v&k=v`，且**没有中文**（正文里不会长这样）
    注意：别用"可读字符占比"单独判 —— URL 串里字母数字很多，占比其实很高 ✗
    """
    # ★ 目录点号行（2026-10-19）：`.............  12` 这种，PDF 目录里成百行 ✗
    #   它们没有信息、还占满上下文，模型读着费劲 → 直接当噪声丢掉 ✓
    #   ⚠ 必须放在「len(line) <= 40 就放行」**之前** ✗
    #     （第一版放后面了 → 点号行都不到 40 字 → 规则永远不生效 ✗
    #      实测后果：花生壳手册前 4 个片段大半是点号，真正的设置步骤被挤出预算）
    if "…" not in line:
        dots = line.count(".") + line.count("·") + line.count("。")
        if dots >= 6 and dots / max(1, len(line)) > 0.5:
            return True
    if len(line) <= 40:
        return False
    if line.count("%") >= 3 and re.search(r"%[0-9A-Fa-f]{2}", line):
        return True
    if line.count("&") >= 3 and "=" in line and not re.search(r"[\u4e00-\u9fff]", line):
        return True
    return _readable_ratio(line) < 0.30


def hard_clean(text: str) -> str:
    """**最后一道防线**：正常清洗后还是编不过时用它（项目主 2026-10-19：
    "扫描前清洗规则请写入规则，不要以后碰到又报错"）。

    比 clean_text 更狠：把一切可能让 tokenizer 挑剔的字符全丢掉 ——
    代理字符、私有区、非字符、控制字符、以及**所有非 BMP 字符**（补充平面）。
    宁可少几个特殊符号，也不让整片文字进不了库 ✓
    """
    if not text:
        return ""
    t = _fix_surrogates(text)
    t = _SAFE_DROP_RE.sub("", t)
    t = "".join(ch for ch in t if ord(ch) <= 0xFFFF)     # 只留基本平面 ✓
    return t.strip()


def clean_text(text: str) -> str:
    """把提取出来的文本洗成"可以放心入库"的样子（索引时默认调用 ✓）。

    规则清单见 `docs/交接说明-多人版.md` 第 ⑬ 节 ✓
    （2026-10-19：编辑时误删过这个函数定义行 ✗ —— 改动清洗相关代码后，
      务必确认 clean_text / hard_clean 两个入口都还在 ✓）
    """
    if not text:
        return ""
    t = _CTRL_RE.sub("", text)
    t = _ZW_RE.sub("", t)
    t = _fix_surrogates(t)          # ★ 孤立代理字符必须清掉（否则整片编不过）✓
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    blank = False
    for raw in t.split("\n"):
        line = raw.strip()
        if not line:
            if not blank and out:
                out.append("")           # 连续空行压成一个 ✓
            blank = True
            continue
        if _is_noise_line(line):
            # ⚠ 丢噪声行时**不能**重置 blank：噪声前后常各有空行，
            #   重置了会多出一个空行 ✗（实测 `零宽字符\n\n\n正文`）
            continue
        blank = False
        out.append(line)
    return "\n".join(out).strip()


def chunk_text(text: str, chunk_chars: int, overlap: int, limit: int) -> list[str]:
    """按字符切块，尽量在换行处断开；超长文本只取前 limit 块。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length and len(chunks) < limit:
        end = min(start + chunk_chars, length)
        if end < length:
            window = text.rfind("\n", start + chunk_chars // 2, end)
            if window == -1:
                window = text.rfind("。", start + chunk_chars // 2, end)
            if window == -1:
                window = text.rfind(". ", start + chunk_chars // 2, end)
            if window != -1:
                end = window + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks
