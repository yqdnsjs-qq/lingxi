"""PP-OCRv6 桥 —— 给「宇之语·灵犀」调用，输出统一 JSON。

用法（必须用这个环境自己的 python）：
    D:\\GJ\\PP-OCRv6\\.venv\\Scripts\\python.exe _lingxi_bridge.py <图片...> [--device gpu:0|cpu]
      --device 默认 gpu:0（快）；显存不够时传 cpu ✓
      --lang   默认 ch（简体）；繁体传 chinese_cht

输出：stdout 一行 JSON
    {"ok":true,"engine":"ppocr","device":"gpu:0","elapsed":1.2,
     "pages":[{"file":"a.png","text":"全文","lines":[{"text":"..","score":0.98,"box":[[x,y],...]}]}]}

设计要点（铁律）：
  · **不改动本项目（PP-OCRv6）的任何原有文件** ✓ 只是新增这一个桥
  · **自己建 PaddleOCR 实例**：它的 get_ocr_model 把 device 写死成 gpu:0 ✗，
    我们要能按需切 CPU/GPU ✓
  · 解析逻辑**复用它的** extract_ocr_result（别自己瞎解析，版本兼容它处理过）✓
  · 任何异常都以 {"ok":false,"error":...} 收场，不把栈抛给调用方 ✓
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--device", default="gpu:0", help="gpu:0 / gpu / cpu")
    ap.add_argument("--lang", default="ch", help="ch / chinese_cht")
    args = ap.parse_args()

    out = {"ok": True, "engine": "ppocr", "device": args.device, "pages": []}
    t_all = time.perf_counter()

    try:
        # 关掉 paddle 那些啰嗦日志（桥的 stdout 只能出 JSON ✓）
        os.environ.setdefault("FLAGS_call_stack_level", "0")
        import app_v6_upgraded as appmod
        from paddleocr import PaddleOCR
        import cv2
        import numpy as np
    except Exception as exc:                                     # noqa: BLE001
        print(json.dumps({"ok": False, "engine": "ppocr",
                          "error": f"import 失败：{type(exc).__name__}: {exc}",
                          "trace": traceback.format_exc()[-600:]},
                         ensure_ascii=False))
        return 1

    # 建引擎：按给定 device 试几种参数写法（paddleocr 3.x 用 device，2.x 用 use_gpu）
    attempts = []
    if args.device.startswith("gpu"):
        attempts = [
            {"lang": args.lang, "device": args.device, "use_textline_orientation": True},
            {"lang": args.lang, "device": "gpu", "use_textline_orientation": True},
            {"lang": args.lang, "use_gpu": True, "use_angle_cls": True},
        ]
    else:
        attempts = [
            {"lang": args.lang, "device": "cpu", "use_textline_orientation": True},
            {"lang": args.lang, "use_gpu": False, "use_angle_cls": True},
        ]
    engine = None
    last_err = None
    t0 = time.perf_counter()
    for kwargs in attempts:
        try:
            engine = PaddleOCR(**kwargs)
            out["init_kwargs"] = {k: str(v) for k, v in kwargs.items()}
            break
        except Exception as err:                                 # noqa: BLE001
            last_err = err
    out["init_seconds"] = round(time.perf_counter() - t0, 2)
    if engine is None:
        print(json.dumps({"ok": False, "engine": "ppocr",
                          "error": f"建引擎失败：{type(last_err).__name__}: {last_err}",
                          "trace": traceback.format_exc()[-600:]},
                         ensure_ascii=False))
        return 1

    for img_path in args.images:
        p = Path(img_path)
        page = {"file": p.name, "text": "", "lines": []}
        try:
            if not p.is_file():
                page["error"] = "文件不存在"
                out["pages"].append(page)
                continue
            # ⚠ cv2.imread **不支持中文路径**（实测：路径含"花生壳手册"就返回 None ✗）
            #   必须用 np.fromfile + cv2.imdecode ✓
            try:
                buf = np.fromfile(str(p), dtype=np.uint8)
                arr = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
            except Exception:                                    # noqa: BLE001
                arr = cv2.imread(str(p))
            if arr is None:
                page["error"] = "图片读不了"
                out["pages"].append(page)
                continue
            t0 = time.perf_counter()
            # 复用它的调用方式与解析（3.x 用 predict，2.x 用 ocr + cls）
            if hasattr(engine, "predict"):
                result = engine.predict(arr)
            else:
                result = engine.ocr(arr, cls=True)
            texts, scores, polys = appmod.extract_ocr_result(result)
            page["elapsed"] = round(time.perf_counter() - t0, 2)
            lines = []
            for i, txt in enumerate(texts or []):
                box = None
                if polys is not None and i < len(polys):
                    try:
                        box = [[float(x), float(y)] for x, y in polys[i]]
                    except Exception:                            # noqa: BLE001
                        box = None
                lines.append({
                    "text": str(txt),
                    "score": round(float(scores[i]), 4) if scores is not None
                             and i < len(scores) else None,
                    "box": box,
                })
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
