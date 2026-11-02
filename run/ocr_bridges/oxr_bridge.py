"""OXR 桥：把图片 / PDF 交给本地 OXR 解析服务，输出灵犀统一结构。

## 为什么用"HTTP 桥"而不是本地跑模型

OXR（SoMarkAI）是 7B 级版面理解模型 + D-FINE 布局分析，官方只支持 Linux + 24GB 显存。
本机把它单独部署在 `D:\\GJ\\AIOXR`（三个服务：解析 8000 / 模型 8080 / 布局 8081），
灵犀这边只要发个 HTTP 请求就行 —— 不占灵犀自己的显存，也不会互相干扰 ✓

## 用法

    python oxr_bridge.py <图片1> <图片2> ... [--api http://127.0.0.1:8000]
                         [--formats markdown,json] [--timeout 600]

最后一行输出统一 JSON（灵犀的 app/ocr.py 只读最后一行）：

    {"ok":true,"engine":"oxr","pages":[{"file":"a.png","text":"# 标题\\n..."}]}

失败时：

    {"ok":false,"engine":"oxr","error":"...","pages":[]}

## 注意

- 只监听本机，api_key 随便填（本地服务不校验）
- 一张图约 10 秒，一页 PDF 约 30 秒（RTX 3060 Ti 8GB，串行推理）→ 超时要给够
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_API = "http://127.0.0.1:8000"


def _post_file(api: str, path: Path, formats: list[str], timeout: float) -> dict:
    """把单个文件 POST 到 /v1/parse/sync，返回解析后的 JSON。"""
    boundary = "----lingxi" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))

    def file_part(name: str, filename: str, data: bytes, mime: str) -> None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode("utf-8"))
        parts.append(data)
        parts.append(b"\r\n")

    field("api_key", "dummy")
    for fmt in formats:
        field("output_formats", fmt)
    file_part("file", path.name, path.read_bytes(), ctype)
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        api.rstrip("/") + "/v1/parse/sync", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _extract(payload: dict) -> tuple[str, list[dict]]:
    """从 OXR 的返回里取 markdown 文本（表格是 HTML，保留）和可选的版面行。"""
    data = payload.get("data") or {}
    result = data.get("result") or {}
    outputs = result.get("outputs") or {}
    md = outputs.get("markdown") or ""
    if isinstance(md, list):                     # 有些版本给的是分段列表
        md = "\n\n".join(str(x) for x in md)
    lines: list[dict] = []
    layout = outputs.get("json") or outputs.get("layout") or []
    if isinstance(layout, dict):
        layout = layout.get("pages") or layout.get("layout") or []
    if isinstance(layout, list):
        for item in layout:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if not text:
                continue
            box = item.get("bbox") or item.get("box")
            lines.append({"text": text, "score": None, "box": box})
    return md, lines


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--formats", default="markdown")
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args(argv)

    files = [Path(f) for f in args.files]
    if not files:
        print(json.dumps({"ok": False, "engine": "oxr",
                          "error": "没有输入文件", "pages": []}, ensure_ascii=False))
        return 1

    formats = [x.strip() for x in args.formats.split(",") if x.strip()]
    pages: list[dict] = []
    errors: list[str] = []
    t0 = time.time()

    for f in files:
        if not f.is_file():
            errors.append(f"{f.name}: 文件不存在")
            continue
        try:
            payload = _post_file(args.api, f, formats, args.timeout)
        except urllib.error.HTTPError as exc:
            errors.append(f"{f.name}: HTTP {exc.code}")
            continue
        except Exception as exc:                                 # noqa: BLE001
            errors.append(f"{f.name}: {type(exc).__name__}: {exc}")
            continue
        if payload.get("code") not in (0, None):
            errors.append(f"{f.name}: {payload.get('message') or '解析失败'}")
            continue
        text, lines = _extract(payload)
        pages.append({"file": str(f), "text": text.strip(), "lines": lines})

    out = {"ok": bool(pages) and not errors, "engine": "oxr", "pages": pages,
           "elapsed": round(time.time() - t0, 2)}
    if errors:
        out["error"] = "；".join(errors[:3])
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
