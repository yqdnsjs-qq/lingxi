"""下载模型到本文件夹。

用法：
    python app\\download_model.py            # 直连，失败自动尝试镜像
    python app\\download_model.py --force    # 忽略已存在的文件，重新校验
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402

REQUIRED_FILES = ("config.json", "model.safetensors", "tokenizer.json")


def _missing(model_dir: Path) -> list[str]:
    return [name for name in REQUIRED_FILES if not (model_dir / name).exists()]


def _set_endpoint(endpoint: str | None) -> None:
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint
    else:
        os.environ.pop("HF_ENDPOINT", None)


def _via_hub(cfg: Config, endpoints: list, force: bool) -> tuple[bool, Exception | None]:
    """先用 huggingface_hub 下（带断点续传和镜像回退）。"""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # 依赖还没装好
        return False, exc

    model_dir = cfg.model_dir
    last_error: Exception | None = None
    for endpoint in endpoints:
        label = endpoint or "https://huggingface.co（直连）"
        _set_endpoint(endpoint)
        print(f"[下载] huggingface_hub · 来源：{label}")
        try:
            snapshot_download(
                repo_id=cfg.repo_id,
                local_dir=str(model_dir),
                max_workers=cfg.workers,
                force_download=force,
                resume_download=True,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[失败] {label} -> {exc}")
            continue
        if not _missing(model_dir):
            return True, None
        print(f"[警告] 仍缺少：{', '.join(_missing(model_dir))}")
    return False, last_error


def download(cfg: Config, force: bool = False) -> int:
    model_dir = cfg.model_dir
    model_dir.mkdir(parents=True, exist_ok=True)

    if not force and not _missing(model_dir):
        total = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
        print(f"[跳过] 模型已存在：{model_dir}（{total / 1e6:.1f} MB）")
        return 0

    source = getattr(cfg, "source", "auto")

    from app.fetch_model import download_all

    # 1) 国内源优先时直接用 ModelScope
    if source == "modelscope":
        if download_all(cfg, source="modelscope", proxy=cfg.proxy or None,
                        connections=cfg.workers, force=force) == 0:
            total = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
            print(f"[完成] 模型已保存到：{model_dir}（{total / 1e6:.1f} MB）")
            return 0

    # 2) HuggingFace：先试 huggingface_hub，再试自带下载器
    if source in ("auto", "huggingface"):
        endpoints: list = [cfg.mirror or None]
        if not cfg.mirror:
            endpoints.append("https://hf-mirror.com")
        else:
            endpoints.append(None)

        ok, hub_error = _via_hub(cfg, endpoints, force)
        if ok:
            total = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
            print(f"[完成] 模型已保存到：{model_dir}（{total / 1e6:.1f} MB）")
            return 0

        print("[提示] huggingface_hub 下载未完成，改用断点续传下载器重试…")
        for endpoint in endpoints:
            if download_all(cfg, source="huggingface", endpoint=endpoint,
                            proxy=cfg.proxy or None, connections=cfg.workers,
                            force=force) == 0:
                return 0
        if source == "huggingface":
            print(f"[错误] 模型下载失败：{hub_error}")
            print("       可在 config.ini 里把 [download] source 改成 modelscope，")
            print("       或填写 mirror / proxy 后重试；重新运行会从断点继续。")
            return 1

    # 3) 兜底：国内 ModelScope 镜像
    print("[提示] HuggingFace 线路不可用，改用 ModelScope 镜像…")
    if download_all(cfg, source="modelscope", proxy=cfg.proxy or None,
                    connections=cfg.workers, force=force) == 0:
        total = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
        print(f"[完成] 模型已保存到：{model_dir}（{total / 1e6:.1f} MB）")
        return 0

    print("[错误] 所有下载源都失败了。")
    print("       可重新运行，已下载的部分会继续续传。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 google/embeddinggemma-2 到本地")
    parser.add_argument("--force", action="store_true", help="强制重新下载/校验")
    args = parser.parse_args()

    cfg = Config()
    cfg.ensure_dirs()
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return download(cfg, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
