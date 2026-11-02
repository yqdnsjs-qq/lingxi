"""每个文件夹自己的索引配置。

模型层面的「加载哪些编码器」是全进程统一的（没法一个文件夹一个模型），
所以这里的 modality 表示「这个文件夹要索引哪些模态的内容」：
某个文件夹要图片就索引图片，另一个只要文字就跳过图片，互不影响。

所有键都可以缺省，缺省即继承 config.ini 里的全局设置。
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("eg2.folderconf")

# ---------------------------------------------------------------- 默认排除目录
# 界面「设置 → 默认排除目录」用它做"恢复建议" ✓
# 用户完全可以自己改；改完对所有文件夹生效 —— 索引和子目录列表都按它过滤 ✓
DEFAULT_IGNORE_DIRS = [
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "out", "target", ".idea", ".vs", ".cache",
    # NAS 系统目录（威联通 NAS）
    ".syncing_db", "@Recently-Snapshot", "@Recycle", ".@__thumb", "@tmp",
    # NAS 系统目录（群晖 Synology）
    "#recycle", "@eaDir", ".snapshot", "#snapshot", ".thumbnails",
    # Windows 系统目录
    "$RECYCLE.BIN", "System Volume Information", "AppData",
    "Windows", "Program Files", "Program Files (x86)",
]

MODE_INHERIT = "inherit"
MODE_CUSTOM = "custom"

# 文件夹级模式 -> 各模态开关
MODE_PRESETS = {
    "full": {"index_text": "yes", "index_images": "yes", "index_audio": "yes",
             "index_video": "yes"},
    "photo": {"index_text": "no", "index_images": "yes", "index_audio": "no",
              "index_video": "no"},
    "text_image": {"index_text": "yes", "index_images": "yes", "index_audio": "no",
                   "index_video": "no"},
    "text_audio": {"index_text": "yes", "index_images": "no", "index_audio": "yes",
                   "index_video": "no"},
    "text_media": {"index_text": "yes", "index_images": "no", "index_audio": "yes",
                   "index_video": "yes"},
    "text": {"index_text": "yes", "index_images": "no", "index_audio": "no",
             "index_video": "no"},
    "name_only": {"index_text": "no", "index_images": "no", "index_audio": "no",
                  "index_video": "no"},
}

MODE_LABELS = {
    MODE_INHERIT: "跟随全局设置",
    "full": "全模态（文字+图片+音频+视频）",
    "photo": "相册（只索引图片，含人脸）",
    "text_image": "文字 + 图片",
    "text_audio": "文字 + 音频",
    "text_media": "文字 + 音视频",
    "text": "仅文字（最快）",
    "name_only": "只索引文件名",
    MODE_CUSTOM: "自定义",
}

# 可单独覆盖的项
KEYS = {
    "mode": {"type": "select", "options": [MODE_INHERIT, *MODE_PRESETS, MODE_CUSTOM],
             "default": MODE_INHERIT},
    "index_text": {"type": "switch"},
    "index_images": {"type": "switch"},
    "index_audio": {"type": "switch"},
    "index_video": {"type": "switch"},
    "index_dim": {"type": "select", "options": [128, 256, 512, 768]},
    "chunk_chars": {"type": "number", "min": 200, "max": 4000},
    "chunk_overlap": {"type": "number", "min": 0, "max": 1000},
    "max_chunks_per_file": {"type": "number", "min": 1, "max": 200},
    "max_file_mb": {"type": "number", "min": 1, "max": 2000},
    "batch_size": {"type": "number", "min": 1, "max": 128},
    "fast_scan": {"type": "switch", "label": "快速扫描（跳过没变过的目录）",
                  "help": "网络共享（NAS）上很值：目录 mtime 没变就不下去列目录，\n"
                          "实测能把几十分钟的扫描压到几秒。\n"
                          "代价：**只改文件内容、不动目录**的那种修改不会被发现\n"
                          "（新建/改名/删除都能发现 ✓）。\n"
                          "想全量重扫就点「重建索引」——它永远忽略这个开关 ✓"},
    "exclude": {"type": "text", "label": "排除规则",
                "help": "只对这个文件夹生效，一行一条（也可逗号分隔）。写法：\n"
                        "  .tmp        按扩展名排除\n"
                        "  node_modules  按名字排除（任意层级）\n"
                        "  备份/*       按相对路径排除\n"
                        "  *草稿*       支持通配符"},
    "use_kb": {"type": "switch", "label": "作为知识库",
               "help": "勾选后，这个文件夹的文字片段会喂给对话模型做知识库问答。"
                       "照片文件夹不要勾 —— 它们几乎没文字，勾了只会拖慢检索。"},
    # ★ 2026-10-19 项目主选定的方案：「每个文件夹单独设（三档都保留，逐个选）」
    #   打标范围也按他说的"1 和 3"：文字版只标筛选后的真插图；
    #   扫描版只做整页 OCR 文字，**不另外标图** ✓
    # ★ 2026-10-19：扫描版 OCR 的**文件夹级开关**
    #   全局默认关（config.ini 的 ocr_enabled=no）；某个文件夹想开就在界面勾 ✓
    #   勾了之后：这个文件夹里"提不出文字"的 PDF 会走 OCR（受页数上限约束）✓
    "ocr_enabled": {
        "type": "switch",
        "label": "扫描版 PDF 走 OCR",
        "help": "这个文件夹里**提不出文字**的 PDF（扫描件、图片型 PDF）会自动做文字识别，"
                "识别出来的文字正常进知识库、能检索能问答 ✓\n"
                "· 不勾 —— 只索引文件名（默认）\n"
                "· 勾上 —— 自动 OCR（吃显存/慢，约 0.4~2.4 秒/页）\n"
                "⚠ 受全局「页数上限」约束（默认 100 页）：超了只做前 N 页，"
                "文件备注里会写明共多少页、还剩多少没做；\n"
                "   想全做就改 config.ini 的 search.ocr_max_pages，或改设置里的值 ✓\n"
                "⚠ 已经有文字层的 PDF 不受影响（直接提取，不 OCR）✓",
    },
    "caption_model": {
        "type": "select",
        "options": ["local", "remote"],
        "default": "",
        "labels": {
            "": "跟随全局（默认走外部 API）",
            "local": "本地模型（不出机器，隐私）",
            "remote": "外部 API（快、强，内容会发到外部）",
        },
        "label": "打标用哪个模型",
        "help": "决定**这个文件夹里的插图/描述交给谁来做**，用来控制隐私 ✓\n"
                "· 本地模型 —— 图片**不离开这台机器**（适合证件、私密资料、"
                "公司内部文档）；代价：要占显存，跑的时候嵌入模型会被临时挪到 CPU\n"
                "· 外部 API —— 把图片发给外部模型（就是你现在打标用的那套）；"
                "更快、更准、不占显存，但**内容会出机器** ✓\n"
                "⚠ 与「PDF 插图打标」的档位配合用：\n"
                "   只登记 → 夜里「全力执行」时才真正调用；\n"
                "   自动 → 索引碰到就调 ✓",
    },
    "caption_mode": {
        "type": "select",
        "options": ["off", "queue", "auto"],
        "default": "off",
        "labels": {
            "": "默认（关闭）",
            "off": "关闭（不打标）",
            "queue": "只登记（夜间一起跑）",
            "auto": "自动（索引时立刻标）",
        },
        "label": "PDF 插图打标",
        "help": "给 PDF 里的**插图**（接线图、界面截图、状态灯说明那种）写中文描述，"
                "入库后能按内容搜到，并标注「第几页」。\n"
                "· 关闭 —— 不做（默认）\n"
                "· 只登记 —— 索引时**只把该标的图登记进队列**（不调用模型、秒过）；"
                "夜里点「全力执行」才真正打标 ✓ 配合你的作息\n"
                "· 自动 —— 索引碰到就当场标（占显存、调外部接口，白天会变慢）\n"
                "⚠ 只标**筛选后的真插图**（去掉图标/装饰/重复，每个文件最多 "
                "ocr_max_images 张，默认 30）；\n"
                "⚠ 纯扫描版 PDF（整页一张大图）走整页 OCR 取文字，不在这里标图 ✓"},
    "subcats": {"type": "subcats", "label": "子目录归类",
                "help": "给**第一层**子目录单独指定用途（只做一层 ✓）。\n"
                        "例：本文件夹=知识库，「照片」=照片库，「素材」=影音库。\n"
                        "没指定的子目录跟随本文件夹 ✓。\n"
                        "⚠ 子目录只改「要不要读文字 / 解码图片 / 当知识库」，"
                        "不改向量维度（同一个文件夹必须同维度 ✓）。\n"
                        "⚠ 子目录改名或移动后这条会失效，界面上会标出来。"},
}

# 勾了「知识库」之后自动套用的推荐值。
# 只在用户**没显式设过**这一项时生效 —— 你手动改过的值不会被覆盖。
KB_PRESET = {
    # 768 维是 EmbeddingGemma 2 的完整质量；256 维是省内存的折中，
    # 实测截断到 256 后最相关的文章排不进前 5。
    "index_dim": 768,
    # 文章类内容比默认 800 字长一些，上下文更完整
    "chunk_chars": 1000,
    "chunk_overlap": 200,
    # 长文别被 40 片截断
    "max_chunks_per_file": 120,
}

_BOOL_KEYS = ("index_text", "index_images", "index_audio", "index_video",
              "use_kb", "fast_scan", "ocr_enabled")
_INT_KEYS = ("index_dim", "chunk_chars", "chunk_overlap", "max_chunks_per_file",
             "max_file_mb", "batch_size")

# ============================================================ 用途类别
# 项目主 2026-10-17 要求：把「添加文件夹」按**用途**归类，每类自带一套
# **经济**的默认扫描口径（能自定义、也能重新分类）。
#
# 两层要分清（这是设计的关键）：
#   · **类别（category）** = 我拿这个文件夹**做什么**（用途层，存在 folders 表）
#   · **扫描模式（mode）**  = 索引**怎么扫**（技术层，存在 config 里）
# 每个类别把 mode + 少量关键开关**预设**好 ✓，用户想改照样能改 ✓
# （改类别只覆盖本表声明的键，用户自己调的其它项一律保留 ✓）。
#
# cost 是"开销档位"，直接显示在界面上，让项目主一眼看出哪个最经济 ✓
_BUILTIN = {
    "find": {
        "label": "文件夹模糊搜索",
        "icon": "🔍",
        "cost": "最经济",
        "desc": "只按文件名/路径找东西：不读内容、不解码图片，建库最快、几乎不占空间。",
        "mode": "name_only",
        "config": {"use_kb": False, "fast_scan": True},
    },
    "photo": {
        "label": "照片库",
        "icon": "🖼",
        "cost": "省",
        "desc": "看图找人：只索引图片并检出人脸（可在「人物」页命名），不解析文档文字。",
        "mode": "photo",
        "config": {"use_kb": False},
    },
    "graphic": {
        "label": "图文库",
        "icon": "📖",
        "cost": "花时间",
        "desc": "图文并茂的笔记/资料：文档文字 + 图片都索引，可搜可看，但不参与问答。",
        "mode": "text_image",
        "config": {"use_kb": False},
    },
    "kb": {
        "label": "知识库",
        "icon": "📚",
        "cost": "花时间",
        "desc": ("内容检索 + 问答：文档文字 + 图片都索引，并作为问答的资料来源"
                 "（可共享给其他用户只读使用）。"),
        # ⚠ 2026-10-18 项目主：知识库默认口径改成**文字 + 图片 + 知识库** ✓
        #   （以前只有 text = 文字，图片搜不到；要图片得自己再勾 ✗）
        "mode": "text_image",
        "config": {"use_kb": True, **KB_PRESET},
    },
    "media": {
        "label": "影音库",
        "icon": "🎬",
        "cost": "花时间",
        "desc": "音视频转写 + 文字，不解码图片。",
        "mode": "text_media",
        "config": {"use_kb": False},
    },
    "all": {
        "label": "全都要",
        "icon": "🗂",
        "cost": "最占空间",
        "desc": "文字 + 图片 + 音频 + 视频全索引。适合体积不大的混装文件夹。",
        "mode": "full",
        "config": {},
    },
}


# ============================================================ 可自定义的类别表
# 项目主 2026-10-18 要的（A + B）：
#   A) 内置 5 个类别的**默认口径可以改**（改完以后新增文件夹就按你改的来 ✓）
#   B) 还能**自己加类别**（比如「扫描件」「设计稿」✓）
#
# 存在一个 json 文件里（不动数据库、不动老配置 ✓）：
#   data/folder_categories.json
#   {
#     "overrides": {"find": {"mode": "text_image"}},      ← A：改内置类的默认口径（只存改过的键）
#     "custom":    [{"id": "scan", "label": "扫描件", ...}] ← B：自己加的类别
#   }
# 生效表 = 内置（套 overrides）+ 自定义 ✓，由 active_categories() 给出 ✓。
_ACTIVE: dict = dict(_BUILTIN)
_ACTIVE_PATH: str = ""


def builtin_categories() -> dict:
    """内置 5 类（出厂默认，不含任何覆盖）"""
    return {k: {**v, "builtin": True} for k, v in _BUILTIN.items()}


def active_categories() -> dict:
    """当前**生效**的类别表（内置 + 覆盖 + 自定义）—— 全模块都从这儿取 ✓"""
    return _ACTIVE


def _clean_cat_entry(raw: dict, cid: str) -> dict | None:
    """清洗一条自定义类别（非法就丢掉，绝不让坏数据进来 ✓）"""
    if not isinstance(raw, dict):
        return None
    label = str(raw.get("label") or "").strip()[:24]
    if not label:
        return None
    mode = str(raw.get("mode") or "").strip().lower()
    if mode not in MODE_PRESETS and mode != MODE_CUSTOM:
        mode = "name_only"                     # 没给合法模式 → 用最经济的 ✓
    conf = _clean_cat_config(raw.get("config"))     # ★ 完整支持 ✓（不再只认 use_kb ✗）
    return {
        "label": label,
        "icon": str(raw.get("icon") or "🏷").strip()[:4] or "🏷",
        "cost": str(raw.get("cost") or "自定义").strip()[:8],
        "desc": str(raw.get("desc") or "").strip()[:200],
        "mode": mode,
        "config": conf,
        "builtin": False,
        "custom": True,
    }


def _clean_cat_config(raw) -> dict:                              # noqa: ANN001
    """清洗**类别**的 config 部分（子目录/类别的模态与调参）✓

    ⚠ 2026-10-18 项目主："自定义那块应该修复好" ——
      以前这里只认 `use_kb` 一个键 ✗，`index_dim`、`chunk_chars`、`fast_scan`
      这些写了也会被悄悄丢掉 ✗，所以类别默认口径**改不动** ✗。现在全支持 ✓
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        if key == "use_kb" or key == "fast_scan":
            out[key] = str(value).strip().lower() in ("1", "yes", "true", "on", "y")
        elif key in ("index_dim", "chunk_chars", "chunk_overlap",
                     "max_chunks_per_file", "max_file_mb", "batch_size"):
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                continue
            if key == "index_dim" and number not in (128, 256, 512, 768):
                continue
            out[key] = number
    return out


def set_categories(data: dict | None) -> dict:
    """套用一份自定义配置（A+B），返回新的生效表 ✓"""
    global _ACTIVE
    data = data if isinstance(data, dict) else {}
    merged: dict = {k: {**v, "builtin": True} for k, v in _BUILTIN.items()}
    # A) 覆盖内置类的默认口径（只认已知键，未知键忽略 ✓）
    for cid, patch in (data.get("overrides") or {}).items():
        cid = str(cid or "").strip().lower()
        if cid not in merged or not isinstance(patch, dict):
            continue
        patch = dict(patch)
        # ★ config 单独处理 ✓（normalize 不认识它 ✗，以前就是在这儿被丢掉的）
        sub = _clean_cat_config(patch.pop("config", None))
        # 容错：手写时把 use_kb 放在外层也能认 ✓（项目主上次就是这么写的）
        for loose in ("use_kb", "fast_scan"):
            if loose in patch:
                sub[loose] = str(patch.pop(loose)).strip().lower() in (
                    "1", "yes", "true", "on", "y")
        clean = normalize(patch)                     # 用同一套清洗 ✓（非法项自动丢）
        unknown = [k for k in patch if k not in KEYS]
        if unknown:
            log.warning("类别 %s 的覆盖里有未知键，已忽略：%s", cid, unknown)
        if sub:
            base_conf = dict(merged[cid].get("config") or {})
            base_conf.update(sub)
            clean["config"] = base_conf
        if clean:
            merged[cid].update(clean)
    # B) 自定义类别（id 不能和内置撞车 ✗）
    for raw in (data.get("custom") or []):
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("id") or "").strip().lower()
        cid = re.sub(r"[^a-z0-9_\-]", "", cid)[:24]
        if not cid or cid in _BUILTIN or cid in merged:
            continue
        entry = _clean_cat_entry(raw, cid)
        if entry:
            merged[cid] = entry
    _ACTIVE = merged
    return _ACTIVE


def load_categories(path) -> dict:  # noqa: ANN001
    """从 json 读自定义配置（文件不存在 = 全用出厂默认 ✓）"""
    global _ACTIVE_PATH
    _ACTIVE_PATH = str(path)
    try:
        if os.path.isfile(_ACTIVE_PATH):
            with open(_ACTIVE_PATH, encoding="utf-8") as fh:
                return set_categories(json.load(fh))
    except Exception as exc:                                     # noqa: BLE001
        log.warning("读 %s 失败，改用出厂默认类别：%s", _ACTIVE_PATH, exc)
    return set_categories(None)


def get_custom_data() -> dict:
    """当前的自定义数据（给界面回显 ✓）"""
    overrides = {}
    for cid, cat in _ACTIVE.items():
        if cid in _BUILTIN:
            # ★ config 也要比 ✓（否则界面上看不到、也存不下来 —— 项目主就是被这个坑的）
            diff = {k: v for k, v in cat.items()
                    if (k in KEYS or k == "config") and _BUILTIN[cid].get(k) != v}
            if diff:
                overrides[cid] = diff
    custom = []
    for cid, cat in _ACTIVE.items():
        if cid not in _BUILTIN:
            custom.append({"id": cid, **{k: v for k, v in cat.items()
                                         if k in ("label", "icon", "cost", "desc",
                                                  "mode", "config")}})
    return {"overrides": overrides, "custom": custom}


def save_categories(data: dict) -> dict:
    """保存并立即生效（写文件用的是"临时文件 + 原子改名"✓ 别写坏配置 ✗）"""
    active = set_categories(data)
    if _ACTIVE_PATH:
        tmp = _ACTIVE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(get_custom_data(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _ACTIVE_PATH)
    return active


def __getattr__(name: str):                                      # noqa: ANN202
    """兼容：外部照旧用 `FC.CATEGORIES` ✓（总是拿到当前生效表 ✓）"""
    if name == "CATEGORIES":
        return _ACTIVE
    raise AttributeError(name)

DEFAULT_CATEGORY = "find"          # 新增文件夹时的默认类别（最经济那个 ✓）
CATEGORY_NONE = ""                 # 未分类：老数据就是这个，行为完全不变 ✓


def categories_info() -> list[dict]:
    """给界面用的类别清单（含每类的默认口径，前端直接渲染 ✓）。"""
    return [{"id": key, "label": c["label"], "icon": c["icon"], "cost": c["cost"],
             "desc": c["desc"], "mode": c["mode"],
             "mode_label": MODE_LABELS.get(c["mode"], c["mode"]),
             "config": dict(c.get("config") or {}),
             # builtin：内置 5 类 = True；自己加的 = False（界面和统计都靠它区分 ✓）
             "builtin": bool(c.get("builtin", key in _BUILTIN))}
            for key, c in active_categories().items()]


def category_label(category: str | None) -> str:
    cat = active_categories().get(str(category or "").strip().lower())
    return cat["label"] if cat else "未分类"


def apply_category(override: dict | None, category: str | None,
                   keep_dim: int | None = None) -> dict:
    """把某个「用途类别」的**经济默认**套到一份配置上。

    规则（关键，别改坏）：
      · 只覆盖**这个类别明确声明**的键 ✓ —— 用户自己调过的其它项
        （排除规则 exclude、快速扫描、分块长度、维度…）一律**保留** ✓
      · 未知/空类别 → 原样返回 ✓（老数据、以及"不分类"都不受影响 ✓）
      · 用户显式传的配置**优先于**类别默认 ✓（调用方按"先类别、后显式"的顺序用）

    `keep_dim`（2026-10-18 项目主）：**沿用这个维度，不用类别默认的** ✓
      场景：文件夹已经扫好了（比如 256 维的 1 万个片段），你只想把它的**用途**
      换个说法（照片库 → 影音库）✗ —— 这时如果类别默认是 768 维，
      就会"改个名字还要整库重建" ✗。给了 keep_dim 就**不动向量空间** ✓
      （维度是向量空间的属性，改它等于让已有片段全废 ✓ 所以能不动就不动 ✓）
    """
    result = dict(normalize(override))
    cat = active_categories().get(str(category or "").strip().lower())
    if not cat:
        return result
    # ★ 关键：类别给的 mode 必须**真正落地**成模态开关 ✓
    #   2026-10-17 验收抓出来的 bug：原来只写 `result["mode"] = ...` ✗，
    #   而 FolderConfig.index_text 的兜底是 True → 「只索引文件名」其实**照样索引正文** ✗
    #   （表现为：本层设成"文件夹模糊搜索"，里面的 .md 还是有内容片段）
    result = apply_mode(result, cat["mode"])
    for key, value in (cat.get("config") or {}).items():
        if key == "index_dim" and keep_dim:
            # ★ 类别想改维度 → 但我们**沿用现有的** ✓
            #   （不是"强制写入 keep_dim" ✗：类别没要求改维度时就完全别碰 ✗，
            #    否则本来一致的配置也被记成"变了"→ 误报需要重建 ✗）
            result[key] = int(keep_dim)
            continue
        result[key] = value
    return normalize(result)


# ============================================================ 子目录归类
# 项目主 2026-10-17 确认：**只做一层** ✓
# 场景：一个大文件夹（比如「D:\notes\wechat」）里的子目录用途不同 ——
# 「照片」该当照片库、「文章」该当知识库、其余只做文件名搜索。
# 做法：不把子目录拆成一条条独立文件夹 ✗（那样太麻烦，项目主明确不要），
# 而是**在同一个文件夹内部**给子目录指定用途 ✓（配置里的 `subcats`）。
#
# ⚠ 关键取舍（也是降风险的地方）：**子目录不改向量维度** ✗
#   同一个文件夹的片段必须同维度（检索要按维度算相似度），让子目录改维度
#   会把一个文件夹的索引搞成混维 ✗。所以子目录只决定
#   **模态开关（文字/图片/音视频）+ 作为知识库** ✓，
#   维度、分块长度这些仍听本文件夹的 ✓。
SUBCAT_MAX_DEPTH = 1
_SUBCAT_SKIP_KEYS = (*_INT_KEYS, "fast_scan")     # 子目录不碰这些（见上面取舍）


def _clean_subcfg(value) -> dict:
    """清洗"子目录自定义配置"：只留模态开关 + 知识库（维度/分块等仍听本文件夹 ✓）"""
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for key in ("index_text", "index_images", "index_audio", "index_video", "use_kb"):
        if key in value:
            out[key] = bool(value[key])
    return out


def clean_subcats(value) -> dict:                                # noqa: ANN001
    """清洗子目录归类表（只保留一层）。

    值可以是两种（2026-10-18 起支持自定义 ✓）：
        "kb"                              预设归类 ✓
        {"index_text": true, ...}         自定义：自己勾选扫什么 ✓
    """
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for raw_name, raw_cat in value.items():
        name = str(raw_name or "").strip().replace("\\", "/").strip("/")
        if not name or "/" in name:            # 只做一层 ✓（多层的忽略）
            continue
        if isinstance(raw_cat, dict):          # ★ 自定义 ✓
            sub = _clean_subcfg(raw_cat)
            if sub:
                out[name] = sub
            continue
        cat = str(raw_cat or "").strip().lower()
        if cat in active_categories():
            out[name] = cat
    return out


def subcat_entry(override: dict | None, rel_path) -> tuple[str, dict]:
    """取"该用哪个子目录口径" → `(类别id, 自定义配置)`。

    返回 `("", {})` = 用本文件夹口径 ✓
         `("kb", {})` = 按预设归类 ✓
         `("custom", {...})` = 用自定义 ✓
    """
    subs = clean_subcats((override or {}).get("subcats"))
    if not subs:
        return "", {}
    parts = [p for p in str(rel_path).replace("\\", "/").split("/") if p]
    if len(parts) < 2:            # 根目录下的文件 → 本层口径 ✓
        return "", {}
    hit = subs.get(parts[0])
    if hit is None:
        return "", {}
    if isinstance(hit, dict):
        return "custom", hit
    return str(hit), {}


def subcat_category(override: dict | None, rel_path) -> str:      # noqa: ANN001
    """（兼容旧调用）只要类别 id。"""
    return subcat_entry(override, rel_path)[0]


def subcat_override(base: dict | None, category: str | None,
                    custom: dict | None = None) -> dict:
    """算出"某个子目录该用的配置"（只换模态 + 知识库开关，见上面的取舍）✓

    `custom`（自定义勾选）优先于 `category`（预设归类）✓
    """
    base = normalize(base)
    if category == "custom" and custom:
        result = dict(base)
        result["mode"] = MODE_CUSTOM
        for key, value in custom.items():
            if key in _SUBCAT_SKIP_KEYS:
                continue
            result[key] = bool(value)
        return normalize(result)
    cat = active_categories().get(str(category or "").strip().lower())
    if not cat:
        return base
    # ★ 同样要让 mode 落地成模态开关 ✓（原因见 apply_category 里那段注释）
    result = apply_mode(base, cat["mode"])
    for key, value in (cat.get("config") or {}).items():
        if key in _SUBCAT_SKIP_KEYS:
            continue                     # 维度/分块/快速扫描：子目录不碰 ✓
        result[key] = value
    return normalize(result)


def normalize(override: dict | None, cfg=None) -> dict:
    """清洗一份文件夹配置：去掉非法项、统一类型。"""
    if not override:
        return {}
    clean: dict = {}
    for key, value in override.items():
        if key not in KEYS:
            continue
        if key in _BOOL_KEYS:
            clean[key] = str(value).strip().lower() in ("1", "yes", "true", "on", "y")
        elif key in _INT_KEYS:
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                continue
            spec = KEYS[key]
            low = spec.get("min", 1)
            high = spec.get("max", 10 ** 9)
            if key == "index_dim" and number not in (128, 256, 512, 768):
                continue
            clean[key] = max(low, min(high, number))
        elif key == "mode":
            text = str(value).strip().lower()
            if text in KEYS["mode"]["options"]:
                clean[key] = text
        elif key == "exclude":
            # 自由文本：保留原样（多行 / 逗号分隔都行），只做长度保护
            if isinstance(value, (list, tuple)):
                value = "\n".join(str(x) for x in value)
            clean[key] = str(value)[:4000]
        elif key == "subcats":
            subs = clean_subcats(value)
            if subs:
                clean[key] = subs
        else:
            # ★ 通用处理（2026-10-19 修）：**任何 options 型字段都按白名单取值** ✓
            #   以前这里是一串硬编码的 `elif key == "mode"` ✗ ——
            #   新加的字段（比如「PDF 插图打标」caption_mode）没有分支，
            #   会被**无声丢掉**：前端点保存返回 200，值却存不进去 ✗
            #   （实测现象：PUT 成功、库里 caption_mode=None）
            spec = KEYS[key]
            opts = spec.get("options")
            if opts:
                text = str(value).strip()
                hit = None
                for o in opts:
                    if text == str(o):
                        hit = o
                        break
                if hit is None:                      # 大小写不敏感地再试一次 ✓
                    for o in opts:
                        if text.lower() == str(o).lower():
                            hit = o
                            break
                if hit is not None:
                    clean[key] = hit
            elif isinstance(value, str):
                clean[key] = value[:200]
    return clean


def apply_mode(override: dict, mode: str) -> dict:
    """选定某个模式时，写入对应的模态开关。"""
    result = dict(override)
    if mode in MODE_PRESETS:
        for key, value in MODE_PRESETS[mode].items():
            result[key] = value == "yes"
    result["mode"] = mode
    return result


def describe(override: dict | None) -> str:
    """给界面显示的一句话说明。"""
    ov = normalize(override)
    if not ov:
        return MODE_LABELS[MODE_INHERIT]
    mode = ov.get("mode")
    if mode in (None, MODE_INHERIT):
        return MODE_LABELS[MODE_INHERIT]
    suffix = ""
    if ov.get("use_kb"):
        dim = ov.get("index_dim") or KB_PRESET["index_dim"]
        suffix = f" · 知识库 {dim} 维"
    if mode != MODE_CUSTOM:
        return MODE_LABELS.get(mode, mode) + suffix
    parts = []
    if ov.get("index_images"):
        parts.append("图片")
    if not ov.get("index_text", True) and ov.get("index_images"):
        return "相册（图片+人脸）" + suffix
    if ov.get("index_audio"):
        parts.append("音频")
    if ov.get("index_video"):
        parts.append("视频")
    if not ov.get("index_text", True):
        parts.append("正文")
    return "自定义 · " + ("+".join(parts) if parts else "仅文件名") + suffix


class FolderConfig:
    """把「文件夹覆盖项 + 全局配置」合成一份可直接用的参数对象。

    索引器和 extract.supports_content() 只认这个对象，不需要知道配置来自哪里。
    """

    def __init__(self, cfg, override: dict | None = None, engine=None):
        self.cfg = cfg
        self.override = normalize(override)
        self.engine = engine

    # ---- 模态能力（由模型实际加载的模态决定）----
    @property
    def use_vision(self) -> bool:
        return self.engine.use_vision if self.engine is not None else self.cfg.use_vision

    @property
    def use_audio(self) -> bool:
        return self.engine.use_audio if self.engine is not None else self.cfg.use_audio

    # ---- 索引范围 ----
    def _get(self, key, fallback):
        value = self.override.get(key)
        if value is not None:
            return value
        # 勾了「知识库」→ 用推荐配置（但用户显式设过的值优先）
        if self.override.get("use_kb") and key in KB_PRESET:
            return KB_PRESET[key]
        return fallback

    @property
    def index_text(self) -> bool:
        return bool(self._get("index_text", True))

    @property
    def index_images(self) -> bool:
        return bool(self._get("index_images", self.cfg.index_images))

    @property
    def index_audio(self) -> bool:
        return bool(self._get("index_audio", self.cfg.index_audio))

    @property
    def index_video(self) -> bool:
        return bool(self._get("index_video", self.cfg.index_video))

    # ---- 插图打标（2026-10-19，按文件夹单独设 ✓）----
    @property
    def caption_mode(self) -> str:
        """PDF 插图打标档位：off（关）/ queue（只登记）/ auto（当场标）。"""
        v = str(self._get("caption_mode", "") or "").strip().lower()
        return v if v in ("off", "queue", "auto") else "off"

    @property
    def caption_model(self) -> str:
        """打标用哪个模型：local（本地，隐私）/ remote（外部 API）/ 空（跟随全局）。"""
        v = str(self._get("caption_model", "") or "").strip().lower()
        return v if v in ("local", "remote") else ""

    @property
    def caption_enabled(self) -> bool:
        return self.caption_mode != "off"

    @property
    def ocr_enabled(self) -> bool:
        """扫描版 PDF 是否走 OCR（文件夹级；不设就跟随全局，默认关）✓"""
        return bool(self._get("ocr_enabled", self.cfg.ocr_enabled))

    # ---- 索引参数 ----
    @property
    def exclude(self) -> list[str]:
        """这个文件夹自己的排除规则（一行一条，或逗号分隔）。"""
        raw = self._get("exclude", "") or ""
        if isinstance(raw, (list, tuple)):
            items = [str(x) for x in raw]
        else:
            items = str(raw).replace("\r", "\n").replace(",", "\n").split("\n")
        return [x.strip() for x in items if x.strip()]

    def excluded(self, path, root, is_dir: bool) -> bool:
        """判断 path 要不要排除。

        规则写法：
            .tmp          纯扩展名
            node_modules  任意层级里叫这个名字的目录/文件
            备份/*        相对路径通配
            *草稿*        名字通配
        """
        import fnmatch

        pats = self.exclude
        if not pats:
            return False
        name = path.name.lower()
        try:
            rel = path.relative_to(root).as_posix().lower()
        except ValueError:
            rel = name
        for raw in pats:
            pat = raw.strip().lower().replace("\\", "/")
            if not pat:
                continue
            if pat.startswith(".") and "*" not in pat and "/" not in pat:
                if path.suffix.lower() == pat:
                    return True
                continue
            if fnmatch.fnmatch(name, pat):
                return True
            if (fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, f"*/{pat}")
                    or fnmatch.fnmatch(rel, f"{pat}/*")):
                return True
        return False

    @property
    def use_kb(self) -> bool:
        """这个文件夹要不要作为对话知识库的来源。"""
        return bool(self._get("use_kb", False))

    @property
    def index_dim(self) -> int:
        return int(self._get("index_dim", self.cfg.index_dim))

    @property
    def chunk_chars(self) -> int:
        return int(self._get("chunk_chars", self.cfg.chunk_chars))

    @property
    def chunk_overlap(self) -> int:
        value = int(self._get("chunk_overlap", self.cfg.chunk_overlap))
        return min(value, self.chunk_chars // 2)

    @property
    def max_chunks_per_file(self) -> int:
        return int(self._get("max_chunks_per_file", self.cfg.max_chunks_per_file))

    @property
    def max_file_mb(self) -> int:
        return int(self._get("max_file_mb", self.cfg.max_file_mb))

    @property
    def search_batch_size(self) -> int:
        return int(self._get("batch_size", self.cfg.search_batch_size))

    @property
    def fast_scan(self) -> bool:
        """快速扫描：跳过 mtime 没变的目录（默认关 ✓，见 KEYS 里的说明）。"""
        return bool(self._get("fast_scan", False))

    # ---- 其它仍走全局的项 ----
    @property
    def ignore_dirs(self):
        return self.cfg.ignore_dirs

    @property
    def skip_ext(self):
        return self.cfg.skip_ext

    def summary(self) -> dict:
        return {
            "mode": self.override.get("mode", MODE_INHERIT),
            "label": describe(self.override),
            "index_text": self.index_text,
            "index_images": self.index_images and self.use_vision,
            "index_audio": self.index_audio and self.use_audio,
            "index_video": self.index_video and self.use_vision,
            "index_dim": self.index_dim,
            "chunk_chars": self.chunk_chars,
            "max_file_mb": self.max_file_mb,
            "max_chunks_per_file": self.max_chunks_per_file,
        }


def needs_vision(folder_configs: list[FolderConfig]) -> bool:
    return any(fc.index_images or fc.index_video for fc in folder_configs)


def needs_audio(folder_configs: list[FolderConfig]) -> bool:
    return any(fc.index_audio for fc in folder_configs)


# 全局模态 -> (是否有视觉, 是否有音频)
MODALITY_KEYS = {
    "full": (True, True),
    "text_image": (True, False),
    "text_audio": (False, True),
    "text": (False, False),
}


def union_modality(global_modality: str, folder_configs: list[FolderConfig]) -> str:
    """把全局模态和各文件夹需求合并，得到真正需要加载的模态。

    不能只按全局设置加载：如果某个文件夹要索引图片，模型就必须带视觉编码器，
    否则那个文件夹会一片空白。
    """
    vision, audio = MODALITY_KEYS.get(global_modality, (True, True))
    vision = vision or needs_vision(folder_configs)
    audio = audio or needs_audio(folder_configs)
    for name, (v, a) in MODALITY_KEYS.items():
        if v == vision and a == audio:
            return name
    return "full"
