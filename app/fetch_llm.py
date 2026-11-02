"""从 ModelScope 下载任意模型（多连接 + 断点续传）。

用法：
    venv\\Scripts\\python.exe app\\fetch_llm.py --repo Qwen/Qwen2.5-3B-Instruct
    venv\\Scripts\\python.exe app\\fetch_llm.py --repo Qwen/Qwen3-4B --connections 8

为什么不用 IDM：IDM 加入队列后有时不启动（进程在跑但字节数不动），
而且它的进度不透明。这里自己下，进度、续传都可控。

关键实现细节（第一版踩过的坑）：
    多个线程**不能共用一个文件句柄** —— 各自的 seek 会互相干扰，
    结果是数据写错位置、而且很快卡死。
    改成：每个分块写自己的 .partNN 文件，全部下完再顺序合并。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

API = ("https://modelscope.cn/api/v1/models/{repo}/repo/files"
       "?Revision=master&Recursive=true")
DL = "https://modelscope.cn/models/{repo}/resolve/master/{path}"

SKIP_SUFFIX = (".md", ".gitattributes", ".txt", ".py", ".h5", ".msgpack", ".onnx")
SKIP_NAME = ("LICENSE", "NOTICE", ".gitattributes")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def list_files(repo: str) -> list[tuple[str, int]]:
    req = urllib.request.Request(API.format(repo=repo),
                                 headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    items = data.get("Data", {}).get("Files") or data.get("Data") or []
    out = []
    for it in items:
        if isinstance(it, str):
            out.append((it, 0))
            continue
        path = it.get("Path") or it.get("Name") or ""
        if not path or it.get("Type") == "tree":
            continue
        out.append((path, int(it.get("Size") or 0)))
    return out


def wanted(path: str) -> bool:
    return not (path.rsplit("/", 1)[-1] in SKIP_NAME
                or path.lower().endswith(SKIP_SUFFIX))


def _head(url: str, known: int = 0) -> tuple[int, bool]:
    """问一下大小与是否支持分块。

    ModelScope 的大文件对 HEAD 经常不回 Content-Length，
    这时就用清单里已知的大小；分块照做（实测它的 resolve 支持 Range）。
    """
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=60) as r:
            size = int(r.headers.get("Content-Length") or 0)
            rng = "bytes" in (r.headers.get("Accept-Ranges") or "").lower()
            return (size or known, rng or (known > 0))
    except Exception:                                     # noqa: BLE001
        return (known, known > 0)


def _fetch(url: str, start: int, end: int, dest: Path) -> None:
    """下载 [start, end] 这段到 dest（独立文件，不共享句柄）。"""
    if dest.exists() and dest.stat().st_size == end - start + 1:
        return
    req = urllib.request.Request(
        url, headers={"User-Agent": "curl/8", "Range": f"bytes={start}-{end}"})
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    if tmp.stat().st_size != end - start + 1:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"分块大小不对 {start}-{end}")
    tmp.replace(dest)


def download(url: str, dest: Path, connections: int = 4,
             known: int = 0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total, ranges_ok = _head(url, known)
    if dest.exists() and total and dest.stat().st_size == total:
        print(f"    已存在 {human(total)}，跳过")
        return
    if not total:
        raise RuntimeError("拿不到文件大小")

    n = max(1, min(connections, 16))
    chunk = max(4 << 20, total // (n * 2))
    parts = [(s, min(s + chunk - 1, total - 1)) for s in range(0, total, chunk)]
    if not ranges_ok and len(parts) > 1:
        parts = [(0, total - 1)]

    done = [0]
    lock = threading.Lock()
    errs: list[str] = []
    files: list[Path] = []

    def worker(i: int, s: int, e: int) -> None:
        p = dest.with_suffix(dest.suffix + f".part{i:03d}")
        try:
            _fetch(url, s, e, p)
            with lock:
                done[0] += e - s + 1
        except Exception as exc:                          # noqa: BLE001
            errs.append(f"块{i}: {exc}")

    # 先统计已存在的分块
    for i, (s, e) in enumerate(parts):
        p = dest.with_suffix(dest.suffix + f".part{i:03d}")
        if p.exists() and p.stat().st_size == e - s + 1:
            done[0] += e - s + 1

    threads = [threading.Thread(target=worker, args=(i, s, e), daemon=True)
               for i, (s, e) in enumerate(parts)]
    stop = threading.Event()

    def report() -> None:
        last, last_t = done[0], time.time()
        while not stop.wait(2.0):
            cur = done[0]
            now = time.time()
            speed = (cur - last) / max(now - last_t, 0.01)
            last, last_t = cur, now
            print(f"      {cur/total*100:5.1f}%  {human(cur)}/{human(total)}  "
                  f"{human(speed)}/s", flush=True)

    rep = threading.Thread(target=report, daemon=True)
    rep.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()

    if errs:
        raise RuntimeError(f"{len(errs)} 个分块失败（重跑会续传）：{errs[0]}")

    # 合并
    print("    合并分块…", flush=True)
    with open(dest, "wb") as out:
        for i in range(len(parts)):
            p = dest.with_suffix(dest.suffix + f".part{i:03d}")
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, 1 << 22)
            p.unlink(missing_ok=True)
    if dest.stat().st_size != total:
        raise RuntimeError(f"合并后大小不对 {dest.stat().st_size} != {total}")
    print(f"    完成 {human(total)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--dest", default=None)
    ap.add_argument("--connections", type=int, default=6)
    args = ap.parse_args()

    dest_dir = (Path(args.dest) if args.dest
                else ROOT / "models" / args.repo.split("/")[-1])
    print(f"  仓库  : {args.repo}")
    print(f"  目标  : {dest_dir}")
    print(f"  连接数: {args.connections}")

    files = [(p, s) for p, s in list_files(args.repo) if wanted(p)]
    if not files:
        print("  没拿到文件清单。")
        return 1
    total = sum(s for _, s in files)
    print(f"  文件  : {len(files)} 个，约 {human(total)}")
    print(flush=True)

    t0 = time.time()
    for i, (path, size) in enumerate(files, 1):
        target = dest_dir / path
        print(f"  [{i}/{len(files)}] {path}  ({human(size)})", flush=True)
        if target.exists() and size and target.stat().st_size == size:
            print("    已存在，跳过")
            continue
        try:
            download(DL.format(repo=args.repo, path=path), target,
                     args.connections, size)
        except Exception as exc:                          # noqa: BLE001
            print(f"    [失败] {exc}")
            print("    再跑一次本命令会从断点继续。")
            return 1

    print()
    print(f"  全部完成，用时 {(time.time()-t0)/60:.1f} 分钟")
    print(f"  目录：{dest_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
