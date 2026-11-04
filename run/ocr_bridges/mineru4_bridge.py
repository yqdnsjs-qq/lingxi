"""MinerU 4.x 的灵犀桥：把一批图片/PDF 交给本地 v4 解析 API，回一段 JSON。

和 v3 的桥（官方 `submit_mineru_task.py`，POST `/file_parse`）不同，v4 换成了
**异步任务**：`POST /v1/tasks` 声明文件 → `PUT` 上传字节 → 轮询 `GET /v1/tasks/{id}`
→ `GET` 结果（zip 里的 `markdown.md`）。这个文件把整套流程包起来，
灵犀只看到"给一批图 → 拿回文字"，跟 v3 的用法保持一致 ✓

用法：
    python mineru4_bridge.py <文件...> --api http://127.0.0.1:8100 \
        --tier basic --ocr-mode ocr

档位（2026-11-05 实测，同一份 3 页表格 PDF）：
    basic    —— 走 pipeline + 表格模型：**表格还原成 Markdown 表格**，正文也好 ✓
    standard —— 走 VLM：竖排/古籍扫描件最准，但**表格会变成图片占位** ✗
    flash    —— 最快，实测扫描件出乱码，不建议给正式入库用
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


def _req(url: str, data: bytes | None = None, method: str = "GET",
         headers: dict | None = None, timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code} {url} → {body}") from exc


def api_ok(api: str, timeout: float = 3.0) -> bool:
    """v4 的解析服务在跑吗（认 /v1/health，避免把 v3/OXR 认错）。"""
    try:
        raw = _req(api.rstrip("/") + "/v1/health", timeout=timeout)
        return b'"ok"' in raw or b'"status":"ok"' in raw.replace(b" ", b"")
    except Exception:                                             # noqa: BLE001
        return False


def _extract_markdown(blob: bytes) -> str:
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.endswith(".md")]
            # 优先顶层 markdown.md，其次随便一个 .md
            pick = next((n for n in names if n.rsplit("/", 1)[-1] == "markdown.md"),
                        names[0] if names else "")
            return zf.read(pick).decode("utf-8", "replace") if pick else ""
    return blob.decode("utf-8", "replace")


def _one_pass(files: list[Path], api: str, tier: str, ocr_mode: str,
              timeout: float) -> dict:
    """跑一档，返回 {"ok","pages","elapsed","error"}。"""
    t0 = time.time()
    items = []
    for p in files:
        if not p.is_file():
            return {"ok": False, "pages": [], "error": f"文件不存在：{p}"}
        items.append({"file_name": p.name, "bytes": p.stat().st_size})

    body = {"tier": tier, "ocr_mode": ocr_mode, "files": items}
    created = json.loads(_req(f"{api.rstrip('/')}/v1/tasks",
                              data=json.dumps(body).encode("utf-8"),
                              method="POST",
                              headers={"Content-Type": "application/json"}))
    task_id = created.get("task_id") or ""
    if not task_id:
        return {"ok": False, "pages": [], "error": f"建任务失败：{created}"}

    # 上传：URL 由服务端给（按 file_name 对上更稳）
    by_name = {f.get("file_name"): f for f in created.get("files") or []}
    for p in files:
        info = by_name.get(p.name) or {}
        url = (info.get("upload") or {}).get("url")
        if not url:
            return {"ok": False, "pages": [], "error": f"没拿到上传地址：{p.name}"}
        _req(url, data=p.read_bytes(), method="PUT",
             headers={"Content-Type": "application/octet-stream"}, timeout=900)

    deadline = time.time() + timeout
    info: dict = {}
    while time.time() < deadline:
        time.sleep(2)
        info = json.loads(_req(f"{api.rstrip('/')}/v1/tasks/{task_id}"))
        if info.get("status") in ("completed", "failed", "cancelled"):
            break
    if info.get("status") != "completed":
        errs = [str((f.get("error") or {}).get("message") or "")
                for f in info.get("files") or [] if f.get("status") == "failed"]
        return {"ok": False, "pages": [], "task_id": task_id,
                "error": (f"任务未完成：{info.get('status')} "
                          f"{'; '.join(e for e in errs if e)}")[:400],
                "elapsed": round(time.time() - t0, 2)}

    pages = []
    for f in info.get("files") or []:
        if f.get("status") != "completed":
            pages.append({"file": f.get("file_name") or "", "text": "",
                          "note": f"失败：{(f.get('error') or {}).get('code')}"})
            continue
        blob = _req(f["result_url"], timeout=600)
        pages.append({"file": f.get("file_name") or "",
                      "text": _extract_markdown(blob)})
    return {"ok": any((p.get("text") or "").strip() for p in pages),
            "pages": pages, "task_id": task_id,
            "elapsed": round(time.time() - t0, 2)}


def parse(files: list[Path], api: str, tier: str, ocr_mode: str,
          timeout: float = 1800, *, fallback_tier: str = "",
          min_chars: int = 20) -> dict:
    """主档 + 兜底档。

    为什么要兜底（2026-11-05 实测）：`basic`（pipeline）能还原表格，但对**竖排/
    古籍扫描件**几乎读不出字（同一页：basic 0 字、standard 495 字）✗；
    而 `standard`（VLM）反过来——竖排最准、表格却变成图片占位 ✗。
    所以默认先 basic（保住表格），字太少再自动跑一遍 standard，取更全的那份 ✓
    """
    t0 = time.time()
    got = _one_pass(files, api, tier, ocr_mode, timeout)
    text_len = sum(len(p.get("text") or "") for p in got.get("pages") or [])
    used = tier
    if fallback_tier and fallback_tier != tier and text_len < int(min_chars) \
            and not got.get("error"):
        alt = _one_pass(files, api, fallback_tier, ocr_mode, timeout)
        alt_len = sum(len(p.get("text") or "") for p in alt.get("pages") or [])
        if alt_len > text_len:
            got, text_len, used = alt, alt_len, fallback_tier
    return {"ok": text_len > 0, "engine": "mineru4",
            "pages": got.get("pages") or [],
            "task_id": got.get("task_id", ""),
            "tier": used, "ocr_mode": ocr_mode,
            "elapsed": round(time.time() - t0, 2),
            "error": None if text_len else (got.get("error") or "没有识别出文字")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="图片或 PDF")
    ap.add_argument("--api", default="http://127.0.0.1:8100")
    ap.add_argument("--tier", default="basic",
                    choices=["flash", "basic", "standard", "advanced"])
    ap.add_argument("--fallback-tier", default="",
                    choices=["", "flash", "basic", "standard", "advanced"],
                    help="主档识别出的字太少时，自动再用这一档跑一遍")
    ap.add_argument("--min-chars", type=int, default=20,
                    help="少于这么多字就触发兜底档（默认 20）")
    ap.add_argument("--ocr-mode", default="ocr",
                    choices=["auto", "txt", "ocr"])
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args()

    if not api_ok(args.api):
        print(json.dumps({"ok": False, "engine": "mineru4", "pages": [],
                          "error": f"MinerU v4 的解析服务没在跑（{args.api}）"},
                         ensure_ascii=False))
        return 2
    got = parse([Path(x) for x in args.files], args.api, args.tier,
                args.ocr_mode, timeout=args.timeout,
                fallback_tier=args.fallback_tier, min_chars=args.min_chars)
    print(json.dumps(got, ensure_ascii=False))     # 最后一行必须是 JSON ✓
    return 0 if got.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
