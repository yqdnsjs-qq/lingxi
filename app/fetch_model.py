"""模型下载器（多连接 + 断点续传，支持 HuggingFace / ModelScope 两个来源）。

huggingface_hub 在断线频繁或国际线路受限的网络里经常失败，本脚本作为兜底：
先要文件清单，再逐个文件多连接分块续传；国内线路推荐 ModelScope 源。

用法：
    python app\\fetch_model.py                     # HuggingFace 官方源
    python app\\fetch_model.py --source modelscope  # 国内 ModelScope 镜像（快）
    python app\\fetch_model.py --source modelscope --connections 8
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.resumable import ResumableDownloader, human  # noqa: E402

# 拿不到接口清单时使用的兜底文件列表
FALLBACK_FILES = [
    ".gitattributes", "1_Pooling/config.json", "2_Normalize/config.json", "README.md",
    "chat_template.jinja", "config.json", "config_sentence_transformers.json",
    "model.safetensors", "modules.json", "preprocessor_config.json",
    "processor_config.json", "sentence_bert_config.json", "tokenizer.json",
    "tokenizer.model", "tokenizer_config.json",
]
REQUIRED = ("config.json", "model.safetensors", "tokenizer.json")

SOURCES = ("huggingface", "modelscope")


def make_opener(proxy: str | None):
    """proxy 为空时显式禁用代理（否则 urllib 会用 Windows 系统代理）。"""
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def _read_json(opener, url: str):
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                     timeout=40) as resp:
        return json.load(resp)


def list_files(opener, source: str, repo_id: str) -> list[tuple[str, int]]:
    """返回 [(文件相对路径, 字节数)]。"""
    if source == "modelscope":
        api = (f"https://modelscope.cn/api/v1/models/{repo_id}"
               f"/repo/files?Revision=master&Recursive=true")
        data = _read_json(opener, api)
        files = data.get("Data", {}).get("Files") or []
        out = [(f["Path"], int(f.get("Size") or 0)) for f in files if f.get("Type") != "tree"]
    else:
        api = f"https://huggingface.co/api/models/{repo_id}?blobs=true"
        data = _read_json(opener, api)
        out = [(s["rfilename"], int(s.get("size") or 0)) for s in data.get("siblings", [])]
    if not out:
        raise RuntimeError(f"{source} 没有返回文件清单")
    return out


def file_url(source: str, repo_id: str, path: str, endpoint: str | None = None) -> str:
    if source == "modelscope":
        base = (endpoint or "https://www.modelscope.cn").rstrip("/")
        return f"{base}/models/{repo_id}/resolve/master/{path}"
    base = (endpoint or "https://huggingface.co").rstrip("/")
    return f"{base}/{repo_id}/resolve/main/{path}"


def _verify(path: Path) -> bool:
    """简单内容校验：JSON 必须能解析，safetensors 头部长度必须合理。"""
    name = path.name.lower()
    if name.endswith(".json"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
            return True
        except (OSError, ValueError, UnicodeDecodeError):
            return False
    if name.endswith(".safetensors"):
        try:
            with open(path, "rb") as handle:
                header = int.from_bytes(handle.read(8), "little")
            return 0 < header < 200_000_000
        except OSError:
            return False
    return True


def download_all(cfg: Config, *, source: str = "huggingface", endpoint: str | None = None,
                 proxy: str | None = None, connections: int = 4, force: bool = False) -> int:
    target = cfg.model_dir
    target.mkdir(parents=True, exist_ok=True)
    opener = make_opener(proxy)

    print(f"  来源：{source}（{endpoint or '默认地址'}）")
    print(f"  目录：{target}")
    try:
        files = list_files(opener, source, cfg.repo_id)
    except Exception as exc:  # noqa: BLE001
        print(f"      读取文件清单失败（{type(exc).__name__}: {exc}），改用内置清单")
        files = [(name, 0) for name in FALLBACK_FILES]

    files.sort(key=lambda item: -item[1])  # 大文件先下，早点暴露网络问题
    failed: list[str] = []
    for name, size in files:
        dest = target / name
        if dest.exists() and dest.stat().st_size > 0 and (not size or dest.stat().st_size == size):
            if not force and _verify(dest):
                print(f"      [跳过] {name}（{human(dest.stat().st_size)}）")
                continue
            print(f"      [重下] {name}（内容校验未通过）")
            try:
                dest.unlink()
            except OSError:
                pass
        if force and dest.exists():
            dest.unlink()
        print(f"      [下载] {name}" + (f"（{human(size)}）" if size else ""))
        url = file_url(source, cfg.repo_id, name, endpoint)
        try:
            ResumableDownloader(
                url, dest, proxy=proxy, connections=connections,
                block_size=2 * 1024 * 1024 if (size and size < 8_000_000) else 4 * 1024 * 1024,
            ).run()
            if size and dest.stat().st_size != size:
                raise RuntimeError(f"大小不符：{dest.stat().st_size} != {size}")
            if not _verify(dest):
                dest.unlink(missing_ok=True)
                raise RuntimeError("内容校验未通过（可能下载到了错误内容）")
        except Exception as exc:  # noqa: BLE001
            print(f"      失败：{exc}")
            failed.append(name)

    missing = [n for n in REQUIRED if not (target / n).exists()]
    if missing or failed:
        print(f"\n  [未完成] 缺失：{', '.join(sorted(set(missing + failed)))}")
        print("           重新运行本脚本会从断点继续。")
        return 1

    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"\n  [完成] 模型已就绪：{target}（{human(total)}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 google/embeddinggemma-2（断点续传）")
    parser.add_argument("--source", choices=SOURCES, default="huggingface")
    parser.add_argument("--endpoint", default=None, help="自定义源地址，例如 https://hf-mirror.com")
    parser.add_argument("--proxy", default=None, help="HTTP 代理")
    parser.add_argument("--connections", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    cfg = Config()
    cfg.ensure_dirs()
    return download_all(cfg, source=args.source, endpoint=args.endpoint,
                        proxy=args.proxy or cfg.proxy or None,
                        connections=args.connections, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
