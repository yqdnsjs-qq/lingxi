"""EmbeddingGemma 2 模型封装。

负责：加载本地模型、把 HTTP 请求里的多模态输入整理成模型可接受的形式、
        批量编码、按需截断维度 / 归一化。
"""
from __future__ import annotations

import base64
import binascii
import gc
import logging
import mimetypes
import os
import re
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger("eg2.model")

# 任务前缀别名 -> SentenceTransformers 里定义的 prompt 名称
PROMPT_ALIASES = {
    "searchquery": "SearchQuery",
    "search": "SearchQuery",
    "query": "SearchQuery",
    "document": "Document",
    "doc": "Document",
    "passage": "Document",
    "questionanswering": "QuestionAnswering",
    "question-answering": "QuestionAnswering",
    "qa": "QuestionAnswering",
    "factchecking": "FactChecking",
    "fact-checking": "FactChecking",
    "coderetrieval": "CodeRetrieval",
    "code": "CodeRetrieval",
    "classification": "Classification",
    "clustering": "Clustering",
    "sentencesimilarity": "SentenceSimilarity",
    "similarity": "SentenceSimilarity",
    "none": "",
    "": "",
}

_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/avif": ".avif",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}

_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,]+)?(?P<b64>;base64)?,(?P<data>.*)$", re.S)

# 模型用这些占位符标记媒体在文本中的位置，数量必须和传入的媒体数一致
PLACEHOLDERS = {"image": "<|image|>", "video": "<|video|>", "audio": "<|audio|>"}
PLACEHOLDER_ORDER = ("image", "video", "audio")


def _media_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple)):
        return sum(1 for item in value if item)
    return 1


def _ensure_placeholders(text: str, media: dict[str, Any]) -> str:
    """文本里缺少媒体占位符时自动补在末尾，避免模型报错。"""
    text = text or ""
    for key in PLACEHOLDER_ORDER:
        need = _media_count(media.get(key))
        if not need:
            continue
        token = PLACEHOLDERS[key]
        missing = need - text.count(token)
        if missing > 0:
            text = (text + " " + " ".join([token] * missing)).strip()
    return text


# 模态 -> (是否加载视觉编码器, 是否加载音频编码器)
MODALITY_KEYS = {
    "full": (True, True),
    "text_image": (True, False),
    "text_audio": (False, True),
    "text": (False, False),
}


class InputError(ValueError):
    """请求里的输入无法解析。"""


class ServiceBusy(RuntimeError):
    """模型正在重新加载，暂时无法编码。"""


_HEIF_READY = False


def decode_image(path: Path, max_side: int = 0):
    """把任意图片文件解成 PIL Image：先 Pillow，不行再用 ffmpeg。

    模型自带的解码器只认 jpeg/png/webp/gif；浏览器也不认 HEIC/TIFF。
    这里统一解好，索引和预览都走这一条路。失败时抛 InputError。

    max_side > 0 时做两件提速的事（**只在内存里，不碰原文件**）：
      1. JPEG 用 draft() 直接按 1/2、1/4、1/8 的尺度解码，
         不用「先全解 12MP 再缩到 1MP」—— 实测 434ms → 197ms。
      2. 缩放用 BILINEAR 而不是默认的 BICUBIC，大图上明显更快。
    """
    try:
        from PIL import Image
    except ImportError as exc:      # pragma: no cover
        raise InputError("没有安装 Pillow，无法读取图片") from exc

    try:
        if path.stat().st_size == 0:
            raise InputError(f"文件是 0 字节，可能还没同步完：{path.name}")
    except OSError as exc:
        raise InputError(f"读取图片失败：{path.name}（{exc}）") from exc

    _register_heif()
    first_error: Exception | None = None
    try:
        with Image.open(path) as img:
            if max_side:
                try:
                    # JPEG 专用：让解码器直接按缩小后的尺度解
                    img.draft("RGB", (max_side, max_side))
                except Exception:                         # noqa: BLE001
                    pass                                  # 其它格式会忽略
            img.load()
            out = img.convert("RGB")
        if max_side and max(out.size) > max_side:
            ratio = max_side / max(out.size)
            out = out.resize(
                (max(1, int(out.width * ratio)), max(1, int(out.height * ratio))),
                Image.BILINEAR)
        return out
    except Exception as exc:  # noqa: BLE001
        first_error = exc

    image = _decode_with_ffmpeg(path)
    if image is not None:
        return image

    from PIL import UnidentifiedImageError

    if isinstance(first_error, UnidentifiedImageError) or first_error is None:
        raise InputError(f"无法识别的图片格式：{path.name}")
    if isinstance(first_error, PermissionError):
        raise InputError(f"没有读取权限：{path.name}")
    raise InputError(f"读取图片失败：{path.name}（{first_error}）") from first_error


def _decode_with_ffmpeg(path: Path):
    """Pillow 认不出来时交给 ffmpeg（PyAV）：相机 RAW、JPEG2000 等它能解。"""
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(str(path)) as container:
            for frame in container.decode(video=0):
                return frame.to_image().convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.info("ffmpeg 也解不了 %s：%s", path.name, exc)
    return None


def _register_heif() -> None:
    """有 pillow-heif 就注册上，这样 iPhone 的 HEIC 照片也能索引。"""
    global _HEIF_READY
    if _HEIF_READY:
        return
    _HEIF_READY = True
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        log.info("已启用 HEIC/HEIF 图片支持")
    except Exception:  # noqa: BLE001
        pass


class EmbeddingEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = None
        self.device = "cpu"
        self.dtype_name = "float32"
        self.dim = cfg.default_dim
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._max_download = 200 * 1024 * 1024  # 单个远程文件最大 200MB
        self._reloading = False
        self._reload_message = ""
        self._reload_error = ""
        self._reload_finished: float | None = None
        # 实际要加载的模态：由「全局设置」和「各文件夹的需求」取并集得出
        self.modality_override: str | None = None
        self.modality_provider = None

    # ------------------------------------------------------------ 模态
    @property
    def modality(self) -> str:
        return self.modality_override or self.cfg.modality

    @property
    def use_vision(self) -> bool:
        return MODALITY_KEYS.get(self.modality, (True, True))[0]

    @property
    def use_audio(self) -> bool:
        return MODALITY_KEYS.get(self.modality, (True, True))[1]

    def refresh_modality(self) -> str:
        """重新计算需要加载的模态（某个文件夹要图片/音频就自动带上对应编码器）。"""
        if self.modality_provider is not None:
            try:
                self.modality_override = self.modality_provider()
            except Exception:  # noqa: BLE001
                log.exception("计算所需模态失败，沿用全局设置")
                self.modality_override = None
        return self.modality

    # ------------------------------------------------------------ 热重载
    @property
    def reloading(self) -> bool:
        return self._reloading

    def reload_state(self) -> dict:
        return {"reloading": self._reloading, "message": self._reload_message,
                "error": self._reload_error, "finished_at": self._reload_finished}

    def reload_async(self) -> bool:
        """在后台按新的 config.ini 重新加载模型（设备/精度/模态都能换）。"""
        if self._reloading:
            return False
        self._reloading = True
        self._reload_message = "正在重新加载模型…"
        self._reload_error = ""
        threading.Thread(target=self._do_reload, name="eg2-reload", daemon=True).start()
        return True

    def _do_reload(self) -> None:
        try:
            with self._lock:
                old = self.model
                self.model = None
                del old
                gc.collect()
                try:
                    import torch

                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001
                    pass
                self.load()
            self._reload_message = "模型已重新加载"
            log.info("模型热重载完成：device=%s dtype=%s modality=%s",
                     self.device, self.dtype_name, self.modality)
        except Exception as exc:  # noqa: BLE001
            self._reload_error = str(exc)
            self._reload_message = f"重新加载失败：{exc}"
            log.exception("模型热重载失败")
        finally:
            self._reloading = False
            self._reload_finished = time.time()

    # ------------------------------------------------------------------ 加载
    def _resolve_device(self, torch):
        want = self.cfg.device
        if want == "cpu":
            return "cpu"
        if want == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("配置要求使用 CUDA，但当前环境检测不到可用显卡")
            return "cuda"
        return "cuda" if torch.cuda.is_available() else "cpu"

    def _resolve_dtype(self, torch, device):
        want = self.cfg.dtype
        if want == "float32":
            return torch.float32, "float32"
        if want == "bfloat16":
            return torch.bfloat16, "bfloat16"
        if want == "float16":
            log.warning("官方明确不建议使用 float16，已自动改为 bfloat16/float32")
        if device == "cuda" and torch.cuda.is_bf16_supported():
            return torch.bfloat16, "bfloat16"
        return torch.float32, "float32"

    def load(self) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        model_path = self.cfg.model_dir
        if not (model_path / "config.json").exists():
            raise FileNotFoundError(
                f"没有找到模型文件：{model_path}\n请先运行 setup.bat 下载模型。"
            )

        # 清掉上次运行（异常退出）残留的临时媒体文件
        try:
            for stale in self.cfg.tmp_dir.glob("*"):
                if stale.is_file():
                    stale.unlink(missing_ok=True)
        except OSError:
            pass

        self.device = self._resolve_device(torch)
        torch_dtype, self.dtype_name = self._resolve_dtype(torch, self.device)

        self.refresh_modality()
        config_kwargs: dict[str, Any] = {}
        if not self.use_vision:
            config_kwargs["vision_config"] = None
        if not self.use_audio:
            config_kwargs["audio_config"] = None

        common = dict(
            device=self.device,
            local_files_only=True,
            trust_remote_code=False,
        )
        attempts = [
            dict(model_kwargs={"dtype": torch_dtype, "low_cpu_mem_usage": True},
                 config_kwargs=config_kwargs),
            dict(model_kwargs={"torch_dtype": torch_dtype, "low_cpu_mem_usage": True},
                 config_kwargs=config_kwargs),
            dict(model_kwargs={"torch_dtype": torch_dtype}, config_kwargs=config_kwargs),
            dict(model_kwargs={"torch_dtype": torch_dtype}),
            dict(),
        ]
        last_err = None
        for kwargs in attempts:
            try:
                self.model = SentenceTransformer(str(model_path), **common, **kwargs)
                break
            except TypeError as exc:  # 参数在不同版本里名字不一样
                last_err = exc
                continue
        if self.model is None:
            raise RuntimeError(f"加载模型失败：{last_err}")

        try:
            self.model.max_seq_length = self.cfg.max_length
        except Exception:
            pass

        getter = getattr(self.model, "get_embedding_dimension", None) or getattr(
            self.model, "get_sentence_embedding_dimension", None
        )
        self.dim = int(getter() or self.cfg.default_dim) if getter else self.cfg.default_dim
        log.info(
            "模型已加载：device=%s dtype=%s modality=%s dim=%d",
            self.device, self.dtype_name, self.modality, self.dim,
        )

    # ------------------------------------------------------------ 前缀/维度
    def resolve_prompt(self, name: Any) -> str:
        raw = self.cfg.default_prompt if name is None else name
        key = str(raw).strip().lower()
        resolved = PROMPT_ALIASES.get(key, str(raw).strip())
        if not resolved:
            return ""
        prompts = self.available_prompts()
        if prompts and resolved not in prompts:
            for candidate in prompts:
                if candidate.lower() == resolved.lower():
                    return candidate
            raise InputError(
                f"不支持的任务前缀“{resolved}”，可用：" + "、".join(prompts)
            )
        return resolved

    def available_prompts(self) -> list[str]:
        try:
            prompts = self.model.prompts
            return sorted(prompts.keys()) if isinstance(prompts, dict) else []
        except Exception:
            return []

    # ------------------------------------------------------------ 输入整理
    def _save_temp(self, raw: bytes, ext: str) -> str:
        self.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
        name = f"{self._pid}-{uuid.uuid4().hex[:12]}{ext}"
        path = self.cfg.tmp_dir / name
        path.write_bytes(raw)
        return str(path)

    def _decode_data_url(self, value: str) -> str:
        m = _DATA_URL_RE.match(value)
        if not m:
            raise InputError("data URL 格式不正确")
        mime = (m.group("mime") or "application/octet-stream").lower()
        payload = m.group("data")
        if m.group("b64"):
            try:
                raw = base64.b64decode(payload, validate=False)
            except (binascii.Error, ValueError) as exc:
                raise InputError(f"base64 解码失败：{exc}") from exc
        else:
            from urllib.parse import unquote_to_bytes

            raw = unquote_to_bytes(payload)
        ext = _MIME_EXT.get(mime) or mimetypes.guess_extension(mime) or ".bin"
        return self._save_temp(raw, ext)

    def _fetch_url(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "embeddinggemma2-local/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read(self._max_download + 1)
            ctype = (resp.headers.get_content_type() or "").lower()
        if len(raw) > self._max_download:
            raise InputError("远程文件超过 200MB 限制")
        ext = _MIME_EXT.get(ctype) or Path(url.split("?")[0]).suffix or ".bin"
        return self._save_temp(raw, ext)

    def _open_image(self, path: Path):
        """解码图片并缩到合适大小，交给模型。

        为什么是 1024 而不是 1600：
        模型把每张图编码成**固定的 280 个 token**（约 816x816 像素），
        所以喂 1600px 进去有一半是白做的 —— 处理器自己还会再缩一次。
        降到 1024 后视觉编码结果不变，但解码/重采样成本降一半以上。
        """
        return decode_image(path, self.cfg.image_max_side)

    def _materialize(self, value: Any, kind: str) -> Any:
        """把 data URL / http URL / 本地路径 统一成模型可用的对象。"""
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            items = [self._materialize(v, kind) for v in value]
            return [v for v in items if v is not None]
        if isinstance(value, (bytes, bytearray)):
            ext = ".png" if kind == "image" else (".mp4" if kind == "video" else ".wav")
            return self._save_temp(bytes(value), ext)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.startswith("data:"):
                return self._decode_data_url(text)
            if text.lower().startswith(("http://", "https://")):
                return self._fetch_url(text)
            path = Path(text)
            if not path.is_absolute():
                path = (self.cfg.root / path).resolve()
            if not path.exists():
                raise InputError(f"文件不存在：{text}")
            if kind == "image":
                return self._open_image(path)
            return str(path)
        return value

    def normalize_item(self, item: Any) -> Any:
        """把一条请求输入整理成 str 或模型接受的 dict。"""
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            # OpenAI 风格 content parts
            if "type" in item and any(
                k in item for k in ("text", "image_url", "input_audio", "video_url")
            ):
                return self._from_content_parts([item])
            out: dict[str, Any] = {}
            text = item.get("text")
            if text is None:
                text = item.get("content")
            if isinstance(text, list):
                merged = self._from_content_parts(text)
                if isinstance(merged, str):
                    text = merged
                    out["text"] = text
                else:
                    out.update(merged)
                    text = None
            if text is not None:
                out["text"] = str(text)
            for src, dst in (
                ("image", "image"), ("images", "image"),
                ("audio", "audio"), ("audios", "audio"), ("sound", "audio"),
                ("video", "video"), ("videos", "video"),
            ):
                if src in item and item[src] is not None:
                    materialized = self._materialize(item[src], dst)
                    if materialized:
                        out[dst] = materialized
            if not out:
                raise InputError("无法识别的输入对象，请提供 text / image / audio / video")
            text = _ensure_placeholders(out.get("text") or "", out)
            if text:
                out["text"] = text
            else:
                out.pop("text", None)
            return out
        raise InputError(f"不支持的输入类型：{type(item).__name__}")

    def _from_content_parts(self, parts: list[Any]):
        """支持 OpenAI 的 content 数组写法。"""
        texts: list[str] = []
        media: dict[str, list[Any]] = {"image": [], "audio": [], "video": []}
        for part in parts:
            if isinstance(part, str):
                texts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type") or "").lower()
            if ptype in ("text", "input_text") or ("text" in part and ptype == ""):
                texts.append(str(part.get("text") or ""))
            elif ptype in ("image_url", "input_image"):
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                media["image"].append(url or part.get("url") or part.get("image"))
            elif ptype in ("input_audio", "audio"):
                audio = part.get("input_audio") or part.get("audio")
                if isinstance(audio, dict):
                    data = audio.get("data")
                    fmt = (audio.get("format") or "wav").lstrip(".")
                    if data and not str(data).startswith(("data:", "http", "/")):
                        data = f"data:audio/{fmt};base64,{data}"
                    media["audio"].append(data)
                else:
                    media["audio"].append(audio)
            elif ptype in ("video_url", "input_video", "video"):
                url = part.get("video_url") or part.get("video")
                if isinstance(url, dict):
                    url = url.get("url")
                media["video"].append(url)
            else:
                # 未知类型：有 text 就当年文本
                if part.get("text"):
                    texts.append(str(part["text"]))
        if not any(media.values()):
            return "\n".join(t for t in texts if t)
        out: dict[str, Any] = {}
        if texts:
            out["text"] = "\n".join(t for t in texts if t)
        for key, values in media.items():
            values = [v for v in values if v]
            if values:
                out[key] = values[0] if len(values) == 1 else values
        return out

    # ---------------------------------------------------------------- 编码
    def encode(
        self,
        items: list[Any],
        prompt_name: Any = None,
        dim: int | None = None,
        normalize: bool | None = None,
        batch_size: int | None = None,
    ) -> list[list[float]]:
        if not items:
            return []
        if self._reloading or self.model is None:
            raise ServiceBusy("模型正在重新加载，请等几秒再试")
        prompt = self.resolve_prompt(prompt_name)
        target_dim = int(dim) if dim else None
        if target_dim is not None and target_dim > self.dim:
            raise InputError(f"请求维度 {target_dim} 超过模型输出维度 {self.dim}")
        do_normalize = self.cfg.normalize if normalize is None else bool(normalize)

        prepared = [self.normalize_item(it) for it in items]
        base_kwargs: dict[str, Any] = dict(
            batch_size=batch_size or self.cfg.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=do_normalize,
        )
        if target_dim:
            base_kwargs["truncate_dim"] = target_dim

        # 纯文本和多模态分开编码，避免不同结构混在一个批次里出问题
        plain = [(i, it) for i, it in enumerate(prepared) if isinstance(it, str)]
        media = [(i, it) for i, it in enumerate(prepared) if not isinstance(it, str)]

        results: list[Any] = [None] * len(prepared)
        with self._lock:
            if plain:
                kwargs = dict(base_kwargs)
                if prompt:
                    kwargs["prompt_name"] = prompt
                vectors = self._encode_call([it for _, it in plain], kwargs)
                for (idx, _), row in zip(plain, vectors):
                    results[idx] = row
            if media:
                kwargs = dict(base_kwargs)
                if prompt:
                    kwargs["prompt_name"] = prompt
                vectors = self._encode_call([it for _, it in media], kwargs)
                for (idx, _), row in zip(media, vectors):
                    results[idx] = row
        if media:
            self._cleanup()
        if any(row is None for row in results):
            raise InputError("有输入没有生成向量，请检查输入内容")
        return [[float(x) for x in row] for row in results]

    def _encode_call(self, payload: list[Any], kwargs: dict[str, Any]):
        try:
            vectors = self.model.encode(payload, **kwargs)
        except TypeError:
            for key in ("truncate_dim", "prompt_name"):
                kwargs.pop(key, None)
            vectors = self.model.encode(payload, **kwargs)
        if hasattr(vectors, "tolist"):
            vectors = vectors.tolist()
        if vectors and isinstance(vectors[0], (int, float)):
            vectors = [vectors]
        return vectors

    def encode_one(self, item: Any, **kwargs) -> list[float]:
        return self.encode([item], **kwargs)[0]

    def count_tokens(self, items: list[Any]) -> int:
        tokenizer = getattr(self.model, "tokenizer", None)
        if tokenizer is None:
            return 0
        total = 0
        for item in items:
            text = item if isinstance(item, str) else (item.get("text") or "")
            if not text:
                continue
            try:
                total += len(tokenizer.encode(str(text), add_special_tokens=False))
            except Exception:
                total += len(str(text).split())
        return total

    # -------------------------------------------------------------- 收尾
    def _cleanup(self) -> None:
        """清掉本次请求落盘的临时媒体文件。"""
        try:
            for f in self.cfg.tmp_dir.glob(f"{self._pid}-*"):
                try:
                    f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def info(self) -> dict:
        info = {
            "model": self.cfg.repo_id,
            "local_dir": str(self.cfg.model_dir),
            "device": self.device,
            "dtype": self.dtype_name,
            "modality": self.modality,
            "configured_modality": self.cfg.modality,
            "dimension": self.dim,
            "max_length": self.cfg.max_length,
            "prompts": self.available_prompts(),
            "pytorch": _torch_version(),
        }
        info.update(self.reload_state())
        return info


def _torch_version() -> str:
    try:
        import torch

        return torch.__version__
    except Exception:
        return "unknown"
