"""命令行编码工具（不想开服务时直接用）。

示例：
    python app\\cli.py "今天天气怎么样"
    python app\\cli.py --prompt SearchQuery --dim 256 "什么是极光"
    python app\\cli.py --image photo.jpg --text "一只猫"
    python app\\cli.py --similar "苹果手机" "iPhone"
    python app\\cli.py --text "整段文本" --save out.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.model import EmbeddingEngine  # noqa: E402


def _load_engine(cfg: Config) -> EmbeddingEngine:
    engine = EmbeddingEngine(cfg)
    print("正在加载模型…", file=sys.stderr)
    started = time.perf_counter()
    engine.load()
    print(f"模型就绪（{time.perf_counter() - started:.1f}s，{engine.device}/{engine.dtype_name}）", file=sys.stderr)
    return engine


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="EmbeddingGemma 2 命令行编码")
    parser.add_argument("texts", nargs="*", help="要编码的文本")
    parser.add_argument("--text", dest="text_opt", help="要编码的文本（单个）")
    parser.add_argument("--image", action="append", default=[], help="图片路径 / URL / data URL，可重复")
    parser.add_argument("--audio", action="append", default=[], help="音频路径 / URL / data URL，可重复")
    parser.add_argument("--video", action="append", default=[], help="视频路径 / URL / data URL，可重复")
    parser.add_argument("--prompt", default=None, help="任务前缀，如 SearchQuery / Document / Classification")
    parser.add_argument("--dim", type=int, default=None, help="输出维度 768/512/256/128")
    parser.add_argument("--no-normalize", action="store_true", help="不做 L2 归一化")
    parser.add_argument("--similar", nargs=2, metavar=("A", "B"), help="计算两段文本的余弦相似度")
    parser.add_argument("--save", help="把向量保存为 JSON 文件")
    parser.add_argument("--full", action="store_true", help="输出完整向量（默认只显示前 8 维）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    cfg = Config()
    cfg.ensure_dirs()
    engine = _load_engine(cfg)

    if args.similar:
        vecs = engine.encode(list(args.similar), prompt_name=args.prompt, dim=args.dim)
        score = _cosine(vecs[0], vecs[1])
        print(f"余弦相似度：{score:.6f}")
        return 0

    if args.image or args.audio or args.video or args.text_opt:
        item: dict = {}
        texts = ([args.text_opt] if args.text_opt else []) + list(args.texts)
        if texts:
            item["text"] = "\n".join(texts)
        for key in ("image", "audio", "video"):
            values = getattr(args, key)
            if values:
                item[key] = values[0] if len(values) == 1 else values
        items = [item]
    elif args.texts:
        items = list(args.texts)
    else:
        parser.print_help()
        return 1

    started = time.perf_counter()
    vectors = engine.encode(items, prompt_name=args.prompt, dim=args.dim,
                            normalize=not args.no_normalize)
    elapsed = time.perf_counter() - started

    if args.save:
        Path(args.save).write_text(json.dumps(vectors, ensure_ascii=False), encoding="utf-8")
        print(f"已保存到 {args.save}")

    if args.json:
        print(json.dumps(vectors, ensure_ascii=False))
        return 0

    for i, vec in enumerate(vectors):
        print(f"#{i} 维度={len(vec)} 耗时={elapsed:.3f}s")
        shown = vec if args.full else vec[:8]
        tail = "" if args.full or len(vec) <= 8 else f" … (共 {len(vec)} 维)"
        print("  [" + ", ".join(f"{x:+.5f}" for x in shown) + "]" + tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
