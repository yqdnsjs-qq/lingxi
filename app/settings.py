"""界面可切换的设置项与「运行模式」预设。

每个设置项都标了生效方式：
  live    立即生效（下一次编码 / 扫描就用新值）
  reload  需要重新加载模型（换设备、精度、模态）
  reindex 需要重建索引（换向量维度）
"""
from __future__ import annotations

import logging

from .config import Config

log = logging.getLogger("eg2.settings")

LIVE, RELOAD, REINDEX = "live", "reload", "reindex"

MODALITY_OPTIONS = [
    {"value": "full", "label": "全模态（文本+图片+音频+视频）"},
    {"value": "text_image", "label": "文本 + 图片"},
    {"value": "text_audio", "label": "文本 + 音频"},
    {"value": "text", "label": "仅文本（最快最省显存）"},
]

SCHEMA = [
    {
        "group": "模型",
        "note": "换设备/精度/模态会让模型重新加载一次（约 40 秒）",
        "items": [
            {"key": "model.modality", "label": "加载的模态", "type": "select",
             "options": MODALITY_OPTIONS, "effect": RELOAD,
             "help": "决定加载哪些编码器，直接影响显存占用"},
            {"key": "model.device", "label": "运行设备", "type": "select", "effect": RELOAD,
             "options": [{"value": "auto", "label": "自动（有显卡就用显卡）"},
                         {"value": "cuda", "label": "只用显卡 CUDA"},
                         {"value": "cpu", "label": "只用 CPU"}],
             "help": "CPU 模式不需要显卡，但会慢很多"},
            {"key": "model.dtype", "label": "数值精度", "type": "select", "effect": RELOAD,
             "options": [{"value": "auto", "label": "自动（显卡 bfloat16 / CPU float32）"},
                         {"value": "bfloat16", "label": "bfloat16"},
                         {"value": "float32", "label": "float32"}],
             "help": "官方明确不建议使用 float16"},
            {"key": "model.batch_size", "label": "嵌入批大小", "type": "number",
             "min": 1, "max": 64, "step": 1, "effect": LIVE,
             "help": "显存不足时调小"},
        ],
    },
    {
        "group": "文件索引",
        "note": "影响下一次建立索引；改向量维度必须先重建索引",
        "items": [
            {"key": "search.index_dim", "label": "新建文件夹的默认向量维度", "type": "select",
             "effect": LIVE,
             "options": [{"value": "128", "label": "128 维（最省，适合纯文本）"},
                         {"value": "256", "label": "256 维（推荐，质量几乎无损）"},
                         {"value": "512", "label": "512 维"},
                         {"value": "768", "label": "768 维（最高精度，慢）"}],
             "help": "只影响以后新加的文件夹；已有文件夹可在它的「配置」里单独改，"
                     "不同维度可以一起搜索，不需要重建"},
            {"key": "search.index_images", "label": "索引图片内容", "type": "switch",
             "effect": LIVE, "help": "打开后可以用“蓝色圆形图案”这类描述搜到图片"},
            {"key": "search.index_audio", "label": "索引音频内容", "type": "switch",
             "effect": LIVE, "help": "比较慢，单段音频要十几秒"},
            {"key": "search.index_video", "label": "索引视频内容", "type": "switch",
             "effect": LIVE, "help": "很慢，建议只对少量视频目录开启"},
            {"key": "search.max_file_mb", "label": "读取正文的大小上限(MB)", "type": "number",
             "min": 1, "max": 500, "step": 1, "effect": LIVE,
             "help": "超过这个大小的文件只索引文件名"},
            {"key": "search.max_chunks_per_file", "label": "单文件最多索引片段数", "type": "number",
             "min": 1, "max": 200, "step": 1, "effect": LIVE,
             "help": "防止超大文件占满索引"},
            {"key": "search.chunk_chars", "label": "文本切块长度(字符)", "type": "number",
             "min": 200, "max": 4000, "step": 100, "effect": LIVE,
             "help": "越小检索越精确，片段数也越多"},
            {"key": "search.batch_size", "label": "索引批大小", "type": "number",
             "min": 1, "max": 128, "step": 1, "effect": LIVE,
             "help": "显存不足时调小"},
            {"key": "search.auto_scan_on_start", "label": "启动时自动增量扫描", "type": "switch",
             "effect": LIVE, "help": "下次启动服务时生效"},
        ],
    },
    {
        "group": "服务",
        "items": [
            {"key": "encoding.default_prompt", "label": "默认任务前缀", "type": "select",
             "effect": LIVE,
             "options": [{"value": "none", "label": "none（不加上下文）"},
                         {"value": "SearchQuery", "label": "SearchQuery（检索查询）"},
                         {"value": "Document", "label": "Document（文档）"},
                         {"value": "Classification", "label": "Classification（分类）"},
                         {"value": "Clustering", "label": "Clustering（聚类）"},
                         {"value": "SentenceSimilarity", "label": "SentenceSimilarity（相似度）"}],
             "help": "只影响 /v1/embeddings 不传 prompt_name 时的默认值"},
            {"key": "server.open_browser", "label": "启动后自动打开浏览器", "type": "switch",
             "effect": LIVE, "help": "下次启动服务时生效"},
        ],
    },
    {
        "group": "多人使用",
        "note": "只对本机生效；服务器上永远按归属隔离，这个开关对它无效",
        "items": [
            {"key": "multi.local_strict", "label": "本机是否也按归属隔离数据",
             "type": "select", "effect": LIVE,
             "options": [
                 {"value": "yes",
                  "label": "是（推荐：只能看自己名下 + 共享的）"},
                 {"value": "no",
                  "label": "否（本机所有人都看到全部，个人专用）"},
             ],
             "help": "多人共用一台电脑时选「是」：普通用户看不到管理员的私有文件夹。"
                     "只影响「能看 / 能改哪些数据」，不影响建库、索引、打标这些本机操作。"
                     "改完需要重启服务生效。"},
        ],
    },
    {
        "group": "服务器连接",
        "note": "只在本机显示：告诉本机往哪台服务器同步（服务器上不需要这个）",
        "local_only": True,
        "items": [
            {"key": "sync.server", "label": "服务器地址", "type": "text",
             "effect": LIVE,
             "placeholder": "http://192.168.1.10:8199",
             "help": "带 http:// 和端口。服务器换了 IP 或换了机器，改这里即可，"
                     "下次同步就用新地址 ✓（改完不用重启）"},
            {"key": "sync.user", "label": "同步账号（可选）", "type": "text",
             "effect": LIVE, "placeholder": "留空用密钥",
             "help": "用密钥访问时可以不填"},
        ],
    },

]

PRESETS = [
    {
        "id": "lite",
        "name": "省显存",
        "desc": "只加载文本模型（270M），图片/音频/视频都不索引",
        "values": {
            "model.modality": "text",
            "model.batch_size": "4",
            "search.index_images": "no",
            "search.index_audio": "no",
            "search.index_video": "no",
            "search.batch_size": "8",
            "search.max_chunks_per_file": "20",
        },
    },
    {
        "id": "balanced",
        "name": "均衡（推荐）",
        "desc": "文本 + 图片，日常找文件最实用",
        "values": {
            "model.modality": "full",
            "model.batch_size": "8",
            "search.index_images": "yes",
            "search.index_audio": "no",
            "search.index_video": "no",
            "search.batch_size": "16",
            "search.max_chunks_per_file": "40",
        },
    },
    {
        "id": "full",
        "name": "全功能",
        "desc": "文本 + 图片 + 音频 + 视频，索引最慢、最占显存",
        "values": {
            "model.modality": "full",
            "model.batch_size": "8",
            "search.index_images": "yes",
            "search.index_audio": "yes",
            "search.index_video": "yes",
            "search.batch_size": "16",
            "search.max_chunks_per_file": "40",
        },
    },
]

_BY_KEY = {item["key"]: item for group in SCHEMA for item in group["items"]}


def current_values(cfg: Config) -> dict[str, str]:
    values = {}
    for key, item in _BY_KEY.items():
        section, _, option = key.partition(".")
        raw = cfg._get(section, option, "").strip()
        if item["type"] == "switch":
            if not raw:
                values[key] = "yes" if _default_bool(item) else "no"
            else:
                values[key] = "yes" if raw.lower() in ("1", "yes", "true", "on", "y") else "no"
        elif not raw:
            values[key] = _default_value(item)
        else:
            values[key] = raw
    return values


def _default_bool(item) -> bool:
    return item["key"] in ("search.index_images", "search.auto_scan_on_start",
                           "server.open_browser")


def _default_value(item) -> str:
    defaults = {
        "model.modality": "full", "model.device": "auto", "model.dtype": "auto",
        "model.batch_size": "8", "search.index_dim": "256",
        "search.max_file_mb": "20", "search.max_chunks_per_file": "40",
        "search.chunk_chars": "800", "search.batch_size": "16",
        "encoding.default_prompt": "none",
    }
    if item["type"] == "switch":
        return "yes" if _default_bool(item) else "no"
    return defaults.get(item["key"], "")


def validate(updates: dict) -> tuple[dict[str, str], list[str]]:
    """过滤非法项，返回 (合法值, 错误说明)。"""
    clean: dict[str, str] = {}
    errors: list[str] = []
    for key, value in updates.items():
        item = _BY_KEY.get(key)
        if item is None:
            errors.append(f"未知设置项：{key}")
            continue
        text = str(value).strip()
        if item["type"] == "switch":
            clean[key] = "yes" if text.lower() in ("1", "yes", "true", "on", "y") else "no"
        elif item["type"] == "select":
            allowed = {opt["value"] for opt in item["options"]}
            if text not in allowed:
                errors.append(f"{item['label']} 只能是 {'/'.join(sorted(allowed))}")
                continue
            clean[key] = text
        elif item["type"] == "number":
            try:
                number = int(float(text))
            except ValueError:
                errors.append(f"{item['label']} 必须是数字")
                continue
            low, high = item.get("min", 0), item.get("max", 10**9)
            if not low <= number <= high:
                errors.append(f"{item['label']} 需要在 {low}-{high} 之间")
                continue
            clean[key] = str(number)
    return clean, errors


def active_preset(cfg: Config) -> str | None:
    values = current_values(cfg)
    for preset in PRESETS:
        if all(values.get(k) == v for k, v in preset["values"].items()):
            return preset["id"]
    return None


def apply(cfg: Config, updates: dict) -> dict:
    """校验并写入 config.ini，返回生效情况。"""
    clean, errors = validate(updates)
    if errors:
        return {"ok": False, "errors": errors}

    effects = {_BY_KEY[key]["effect"] for key in clean}
    cfg.update(clean)
    log.info("设置已更新：%s", ", ".join(f"{k}={v}" for k, v in clean.items()))
    return {
        "ok": True,
        "applied": clean,
        "need_reload": RELOAD in effects,
        "need_reindex": REINDEX in effects,
        "values": current_values(cfg),
        "active_preset": active_preset(cfg),
    }
