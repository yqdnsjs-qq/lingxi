"""自检脚本：确认服务已启动、模型可用、各种输入都能正常编码。

用法（需要先启动服务）：
    python app\\selftest.py
    python app\\selftest.py --url http://127.0.0.1:8199
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [通过] {name} {extra}")
    else:
        FAIL += 1
        print(f"  [失败] {name} {extra}")


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb or 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="EmbeddingGemma 2 本地服务自检")
    parser.add_argument("--url", default=None, help="服务地址，默认按 config.ini 的端口")
    args = parser.parse_args()

    cfg = Config()
    base = (args.url or f"http://127.0.0.1:{cfg.port}").rstrip("/")

    def post(path, payload):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except ValueError:
                return exc.code, {}

    def get(path):
        with urllib.request.urlopen(base + path, timeout=30) as resp:
            return resp.status, resp.read()

    print(f"自检目标：{base}")
    print()

    # 健康检查
    try:
        status, body = get("/health")
        health = json.loads(body)
        check("服务可访问 /health", status == 200)
        check("模型已加载", health.get("dimension") == 768,
              f"device={health.get('device')} dtype={health.get('dtype')} "
              f"modality={health.get('modality')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失败] 连不上服务：{exc}")
        print("         请先双击 start.bat 启动服务。")
        return 1

    # 文本
    status, data = post("/v1/embeddings", {
        "input": ["什么是极光？", "极光由太阳带电粒子撞击高层大气产生。"],
        "prompt_name": "SentenceSimilarity",
    })
    check("文本嵌入", status == 200 and len(data.get("data", [])) == 2, f"status={status}")
    if status == 200:
        vecs = [d["embedding"] for d in data["data"]]
        check("输出 768 维", len(vecs[0]) == 768)
        score = cosine(vecs[0], vecs[1])
        check("两句语义相近", score > 0.7, f"余弦={score:.4f}")

    # Matryoshka 截断
    status, data = post("/v1/embeddings", {"input": "维度截断测试", "dimensions": 256})
    if status == 200:
        vec = data["data"][0]["embedding"]
        norm = math.sqrt(sum(x * x for x in vec))
        check("256 维截断并归一化", len(vec) == 256 and abs(norm - 1) < 0.02, f"模长={norm:.4f}")
    else:
        check("256 维截断并归一化", False, f"status={status}")

    # 图片
    image = cfg.data_dir / "samples" / "sample_image.png"
    if image.exists():
        with open(image, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode()
        status, data = post("/embed", {"input": {
            "text": "示例图片",
            "image": f"data:image/png;base64,{encoded}",
        }})
        check("图片嵌入", status == 200 and data.get("dimension") == 768, f"status={status}")
    else:
        print(f"  [跳过] 图片嵌入（没有示例图片 {image}）")

    # 音频
    audio = cfg.data_dir / "samples" / "sample_audio.wav"
    if audio.exists():
        with open(audio, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode()
        status, data = post("/embed", {"input": {"audio": f"data:audio/wav;base64,{encoded}"}})
        check("音频嵌入", status == 200 and data.get("dimension") == 768, f"status={status}")
    else:
        print(f"  [跳过] 音频嵌入（没有示例音频 {audio}）")

    # 错误处理
    status, _ = post("/v1/embeddings", {"input": "x", "dimensions": 4096})
    check("非法维度被拒绝", status == 400, f"status={status}")

    # 文件搜索
    try:
        with urllib.request.urlopen(base + "/api/folders", timeout=30) as resp:
            folders = json.load(resp)
        check("文件搜索模块可用", "folders" in folders,
              f"已添加 {len(folders.get('folders', []))} 个文件夹，"
              f"已索引 {folders.get('stats', {}).get('files', 0)} 个文件")
        check("文件夹模式接口可用",
              bool(folders.get("modes")) and bool(folders.get("schema")),
              f"{len(folders.get('modes', []))} 种模式 / "
              f"{len(folders.get('schema', {}))} 项可调参数")
        with urllib.request.urlopen(
                base + "/api/search?q=" + urllib.parse.quote("测试"), timeout=120) as resp:
            result = json.load(resp)
        check("搜索接口可用", "results" in result, f"返回 {result.get('count', 0)} 条")

        # 预览接口要存在：随便给个 id，应该是 404 而不是 404 路由缺失/500
        try:
            urllib.request.urlopen(base + "/api/preview/0/text", timeout=30)
            preview_ok, preview_msg = False, "居然返回了 200"
        except urllib.error.HTTPError as exc:
            preview_ok = exc.code == 404
            preview_msg = f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            preview_ok, preview_msg = False, str(exc)[:60]
        check("文件预览接口可用", preview_ok, preview_msg)
    except Exception as exc:  # noqa: BLE001
        check("文件搜索模块可用", False, str(exc)[:80])

    # 设置与运行模式
    try:
        with urllib.request.urlopen(base + "/api/settings", timeout=30) as resp:
            conf = json.load(resp)
        items = sum(len(g["items"]) for g in conf["schema"])
        check("设置接口可用", items > 0,
              f"{items} 个设置项，{len(conf['presets'])} 个预设，"
              f"当前模式：{conf.get('active_preset') or '自定义'}")
    except Exception as exc:  # noqa: BLE001
        check("设置接口可用", False, str(exc)[:80])

    # 网页
    status, body = get("/")
    check("网页测试界面可打开", status == 200 and b"EmbeddingGemma" in body)
    status, body = get("/search")
    check("文件搜索页面可打开", status == 200 and b"search" in body)
    status, body = get("/static/settings.js")
    check("设置面板脚本可加载", status == 200 and b"EG2Settings" in body)

    print()
    print(f"自检结果：通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL == 0:
        print("一切正常，可以开始使用了。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
