"""多连接 + 断点续传的文件下载器。

国际线路不稳定、单连接经常几百 KB 就断开时，用它代替 pip / huggingface_hub
自带的下载逻辑：把文件切成固定大小的块，多线程分块下载，每块完成即落盘，
进度记录在同名 .part.json 里；中断后重新运行会从已完成的位置继续。

只依赖标准库。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

READ_CHUNK = 256 * 1024


def human(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.0f} KB"


class ResumableDownloader:
    def __init__(
        self,
        url: str,
        dest: str | Path,
        *,
        proxy: str | None = None,
        connections: int = 4,
        block_size: int = 4 * 1024 * 1024,
        timeout: int = 30,
        headers: dict | None = None,
        stall_timeout: int = 600,
        quiet: bool = False,
    ):
        self.url = url
        self.dest = Path(dest)
        self.proxy = proxy
        self.connections = max(1, connections)
        self.block_size = max(READ_CHUNK, block_size)
        self.timeout = timeout
        self.headers = {"User-Agent": "eg2-downloader/1.0", **(headers or {})}
        self.stall_timeout = stall_timeout
        self.quiet = quiet
        self.part = Path(str(self.dest) + ".part")
        self.state_path = Path(str(self.dest) + ".part.json")
        self._lock = threading.Lock()
        self._done: set[int] = set()
        self._failed = False
        self._total = 0
        self._nblocks = 0
        self.range_ok = True

    # ------------------------------------------------------------------ 基础
    def _opener(self):
        # 注意：proxy 为空时必须显式禁用代理，
        # 否则 urllib 会去读 Windows 系统代理设置。
        if self.proxy:
            handler = urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
        else:
            handler = urllib.request.ProxyHandler({})
        return urllib.request.build_opener(handler)

    def _log(self, message: str) -> None:
        if not self.quiet:
            print(message, flush=True)

    def _probe(self) -> int:
        op = self._opener()
        req = urllib.request.Request(self.url, headers={**self.headers, "Range": "bytes=0-0"})
        with op.open(req, timeout=self.timeout) as resp:
            content_range = resp.headers.get("Content-Range")
            if content_range and "/" in content_range:
                self.range_ok = True
                return int(content_range.rsplit("/", 1)[1])
            # 服务端忽略了 Range，只能整段顺序下载
            self.range_ok = False
            if resp.headers.get("Content-Length"):
                return int(resp.headers["Content-Length"])
        raise RuntimeError(f"无法确定文件大小：{self.url}")

    def _simple_download(self) -> Path:
        """服务端不支持 Range 时的兜底：单连接顺序下载，断了从断点续。"""
        opener = self._opener()
        # 用独立的进度标记，避免误把「分块模式预分配出来的空文件」当成已下载内容
        marker = Path(str(self.part) + ".simple")
        have = 0
        if marker.exists() and self.part.exists():
            try:
                info = json.loads(marker.read_text(encoding="utf-8"))
                if info.get("url") == self.url and info.get("total") == self._total:
                    have = self.part.stat().st_size
            except (OSError, ValueError):
                have = 0
        if have > self._total:
            have = 0
        if have == 0 and self.part.exists():
            try:
                self.part.unlink()
            except OSError:
                pass

        last_change = time.time()
        started = time.time()
        while (not self._total) or have < self._total:
            headers = dict(self.headers)
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                with opener.open(urllib.request.Request(self.url, headers=headers),
                                 timeout=self.timeout) as resp:
                    if have and resp.status != 206:
                        have = 0
                    mode = "ab" if have else "wb"
                    with open(self.part, mode) as handle:
                        while True:
                            block = resp.read(READ_CHUNK)
                            if not block:
                                break
                            handle.write(block)
                            have += len(block)
                            last_change = time.time()
                            if self._total and have >= self._total:
                                break
            except Exception:  # noqa: BLE001
                pass
            try:
                marker.write_text(json.dumps({"url": self.url, "total": self._total,
                                              "have": have}), encoding="utf-8")
            except OSError:
                pass
            if self._total and have >= self._total:
                break
            if time.time() - last_change > self.stall_timeout:
                raise RuntimeError("长时间没有进展，停止下载（可重新运行继续续传）")
            time.sleep(1)
        size = self.part.stat().st_size
        if self._total and size != self._total:
            raise RuntimeError(f"文件大小不对：{size} != {self._total}")
        os.replace(self.part, self.dest)
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        self._log(f"      下载完成：{self.dest.name}（{human(size)}）")
        return self.dest

    def _load_state(self, total: int) -> None:
        if not (self.state_path.exists() and self.part.exists()):
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if state.get("total") == total and state.get("url") == self.url:
            self._done = {int(i) for i in state.get("done", [])}

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps({"url": self.url, "total": self._total,
                            "done": sorted(self._done)}),
                encoding="utf-8",
            )
        except OSError:
            pass

    # --------------------------------------------------------------- 工作线程
    def _next_block(self) -> int | None:
        with self._lock:
            for index in range(self._nblocks):
                if index not in self._done:
                    return index
        return None

    def _worker(self) -> None:
        opener = self._opener()
        while not self._failed:
            index = self._next_block()
            if index is None:
                return
            start = index * self.block_size
            end = min(start + self.block_size, self._total) - 1
            offset = start
            while offset <= end and not self._failed:
                try:
                    request = urllib.request.Request(
                        self.url,
                        headers={**self.headers, "Range": f"bytes={offset}-{end}"},
                    )
                    with opener.open(request, timeout=self.timeout) as resp:
                        if resp.status == 200:
                            # 有些 CDN 对小文件会忽略 Range，直接返回整份内容
                            self._store_whole(resp)
                            return
                        with open(self.part, "r+b") as handle:
                            handle.seek(offset)
                            while offset <= end:
                                block = resp.read(READ_CHUNK)
                                if not block:
                                    break
                                if offset + len(block) > end + 1:
                                    block = block[: end + 1 - offset]
                                handle.write(block)
                                offset += len(block)
                except Exception:  # noqa: BLE001
                    time.sleep(0.5)
            if offset > end:
                with self._lock:
                    self._done.add(index)
                self._save_state()

    # ------------------------------------------------------------------- 主流程
    def _store_whole(self, resp) -> None:
        """把整份响应内容写入 .part，并标记全部块完成。"""
        tmp = Path(str(self.part) + ".full")
        with open(tmp, "wb") as handle:
            while True:
                block = resp.read(READ_CHUNK)
                if not block:
                    break
                handle.write(block)
        size = tmp.stat().st_size
        if self._total and size != self._total:
            tmp.unlink(missing_ok=True)
            return
        for _ in range(5):
            try:
                os.replace(tmp, self.part)
                break
            except OSError:
                time.sleep(0.5)
        with self._lock:
            self._done.update(range(self._nblocks))
        self._save_state()

    def run(self) -> Path:
        if self.dest.exists():
            self._log(f"      已存在，跳过：{self.dest.name}")
            return self.dest

        self._total = self._probe()
        self._nblocks = (self._total + self.block_size - 1) // self.block_size
        self._load_state(self._total)

        if not self.range_ok or self._nblocks <= 1:
            self._log(f"      文件大小 {human(self._total)}，单连接顺序下载")
            self.part.parent.mkdir(parents=True, exist_ok=True)
            return self._simple_download()

        self._log(f"      文件大小 {human(self._total)}，切成 {self._nblocks} 块，"
                  f"{self.connections} 个连接并发")

        self.part.parent.mkdir(parents=True, exist_ok=True)
        if not self.part.exists() or self.part.stat().st_size != self._total:
            with open(self.part, "wb") as handle:
                handle.truncate(self._total)

        if len(self._done) >= self._nblocks:
            return self._finish()

        threads = [threading.Thread(target=self._worker, daemon=True)
                   for _ in range(self.connections)]
        for thread in threads:
            thread.start()

        last_done = len(self._done)
        last_change = time.time()
        started = time.time()
        while any(thread.is_alive() for thread in threads):
            time.sleep(2)
            finished = len(self._done)
            if finished != last_done:
                last_done = finished
                last_change = time.time()
                elapsed = max(time.time() - started, 0.1)
                bytes_done = min(self._total, finished * self.block_size)
                speed = bytes_done / elapsed
                self._log(f"        {bytes_done * 100 / self._total:5.1f}%  "
                          f"{human(bytes_done)} / {human(self._total)}  "
                          f"{speed / 1e6:.2f} MB/s")
                self._save_state()
            elif time.time() - last_change > self.stall_timeout:
                self._failed = True
                self._log("      长时间没有进展，停止下载（可重新运行继续续传）")
                break

        for thread in threads:
            thread.join(timeout=5)

        if self._failed or len(self._done) < self._nblocks:
            missing = self._nblocks - len(self._done)
            raise RuntimeError(f"下载未完成，还差 {missing} 块，重新运行可继续续传")
        return self._finish()

    def _finish(self) -> Path:
        size = self.part.stat().st_size
        if size != self._total:
            raise RuntimeError(f"文件大小不对：{size} != {self._total}")
        os.replace(self.part, self.dest)
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._log(f"      下载完成：{self.dest.name}（{human(size)}）")
        return self.dest
