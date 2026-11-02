"""RapidOCR 桥 —— 给「宇之语·灵犀」调用，输出统一 JSON。

用法（必须用这个环境自己的 python）：
    D:\\GJ\\lingxi-ocr-rapid\\.venv\\Scripts\\python.exe bridge.py <图片路径> [更多图片...]
输出：stdout 一行 JSON
    {"ok":true,"engine":"rapid","elapsed":1.2,
     "pages":[{"file":"a.png","text":"全文","lines":[{"text":"..","score":0.98,"box":[[x,y],...]}]}]}

设计要点（铁律）：
  · **绝不改动调用方的文件** ✓ 只读图片、只往 stdout 写结果
  · 纯 CPU（onnxruntime），**不占显存** ✓ 不会和嵌入模型抢 GPU
  · 只用标准库 + rapidocr，出任何问题都以 {"ok":false,"error":...} 收场，不抛栈到调用方
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        print(json.dumps({"ok": False, "error": "没有给图片路径"}, ensure_ascii=False))
        return 2

    out = {"ok": True, "engine": "rapid", "pages": []}
    t_all = time.perf_counter()
    try:
        from rapidocr_onnxruntime import RapidOCR
        t0 = time.perf_counter()
        engine = RapidOCR()
        out["init_seconds"] = round(time.perf_counter() - t0, 2)
    except Exception as exc:                                     # noqa: BLE001
        print(json.dumps({"ok": False, "engine": "rapid",
                          "error": f"初始化失败：{type(exc).__name__}: {exc}",
                          "trace": traceback.format_exc()[-800:]},
                         ensure_ascii=False))
        return 1

    for p in paths:
        page = {"file": p.name, "text": "", "lines": []}
        try:
            if not p.is_file():
                page["error"] = "文件不存在"
                out["pages"].append(page)
                continue
            t0 = time.perf_counter()
            result, _elapse = engine(str(p))
            page["elapsed"] = round(time.perf_counter() - t0, 2)
            lines = []
            for item in (result or []):
                # rapidocr 返回 (box, text, score)
                try:
                    box, text, score = item[0], item[1], item[2]
                except Exception:                                # noqa: BLE001
                    continue
                box = [[float(x), float(y)] for x, y in box]
                lines.append({"text": str(text), "score": round(float(score), 4),
                              "box": box})
            page["lines"] = lines
            page["text"] = "\n".join(x["text"] for x in lines)
        except Exception as exc:                                 # noqa: BLE001
            page["error"] = f"{type(exc).__name__}: {exc}"
        out["pages"].append(page)

    out["elapsed"] = round(time.perf_counter() - t_all, 2)
    out["total_chars"] = sum(len(x.get("text") or "") for x in out["pages"])
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
