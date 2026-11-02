"""断点续传安装 PyTorch 显卡版。

pip 自带的下载器在国内网络下经常中途断开、大 wheel 反复重下。本脚本改成：
  1. 从 PyTorch 官方索引里挑出与本机 Python 匹配的最新 wheel；
  2. 用 app.resumable 多连接 + 分块断点续传把它拉到 .cache\\wheels；
  3. 让 pip 从本地文件安装（不再走网络下载 2GB 的包）。

用法（一般由 setup.bat 自动调用）：
    python app\\fetch_torch.py                          # 依次尝试 cu130 / cu128 / cu126 / cpu
    python app\\fetch_torch.py --indexes cu130 --connections 4
    python app\\fetch_torch.py --proxy http://127.0.0.1:10810
    python app\\fetch_torch.py --download-only           # 只下载不安装
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.resumable import ResumableDownloader, human  # noqa: E402

BASE = "https://download.pytorch.org/whl"
DEFAULT_INDEXES = ("cu130", "cu128", "cu126", "cpu")


def make_opener(proxy: str | None):
    """proxy 为空时显式禁用代理（否则 urllib 会用 Windows 系统代理）。"""
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def plat_tags() -> tuple[str, str]:
    """返回当前 Python 的 wheel 标签，例如 ('cp312', 'win_amd64')。"""
    import sysconfig

    tag = "cp%d%d" % sys.version_info[:2]
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        tag += "t"
    if sys.platform == "win32":
        arch = "win_amd64"
    elif sys.platform == "darwin":
        arch = "macosx_11_0_arm64" if sysconfig.get_platform().endswith("arm64") else "macosx_10_9_x86_64"
    else:
        arch = "manylinux_2_28_x86_64" if sys.maxsize > 2**32 else "manylinux_2_28_i686"
    return tag, arch


def _version_key(name: str) -> tuple:
    raw = name.split("-")[1] if "-" in name else "0"
    return tuple(int(p) for p in re.findall(r"\d+", raw)) or (0,)


def find_wheel(op, index: str, package: str) -> tuple[str, str]:
    """在索引页里找出当前 Python 可用的最新 wheel，返回 (文件名, 下载地址)。"""
    url = f"{BASE}/{index}/{package}/"
    html = op.open(url, timeout=60).read().decode("utf-8", "replace")
    tag, arch = plat_tags()
    suffix = f"-{tag}-{tag}-{arch}.whl"
    candidates: list[tuple[str, str]] = []
    for href in re.findall(r'href="([^"]+)"', html):
        href = href.split("#", 1)[0]
        name = href.rsplit("/", 1)[-1]
        if name.startswith(package + "-") and name.endswith(suffix):
            candidates.append((name, href))
    if not candidates:
        raise RuntimeError(f"{index} 上没有找到 {package} 的 {tag}/{arch} 版本")
    candidates.sort(key=lambda item: _version_key(item[0]))
    return candidates[-1]


def pip_install(py: str, args: list[str]) -> int:
    command = [py, "-m", "pip", "install", "--disable-pip-version-check",
               "--retries", "15", "--timeout", "180", *args]
    print("      $ " + " ".join(command), flush=True)
    return subprocess.call(command)


def main() -> int:
    parser = argparse.ArgumentParser(description="断点续传下载并安装 PyTorch")
    parser.add_argument("--indexes", nargs="*", default=list(DEFAULT_INDEXES))
    parser.add_argument("--proxy", default=None, help="HTTP 代理，例如 http://127.0.0.1:10810")
    parser.add_argument("--connections", type=int, default=4, help="并发连接数")
    parser.add_argument("--packages", nargs="*", default=["torchvision", "torchaudio"])
    parser.add_argument("--py", default=sys.executable, help="用于安装的 Python 解释器")
    parser.add_argument("--download-only", action="store_true", help="只下载 wheel，不安装")
    args = parser.parse_args()

    cfg = Config()
    cfg.ensure_dirs()
    proxy = args.proxy or cfg.proxy or None
    wheels = cfg.root / ".cache" / "wheels"
    opener = make_opener(proxy)

    print(f"  代理：{proxy or '（直连）'}   并发：{args.connections}")
    print(f"  wheel 目录：{wheels}")

    downloaded: list[Path] = []
    for index in args.indexes:
        print(f"\n  === {index} ===", flush=True)
        try:
            name, link = find_wheel(opener, index, "torch")
        except Exception as exc:  # noqa: BLE001
            print(f"      查询索引失败：{exc}")
            continue
        print(f"      最新版本：{name}")
        target = wheels / name.replace("%2B", "+")
        try:
            wheel = ResumableDownloader(
                link, target, proxy=proxy, connections=args.connections
            ).run()
        except Exception as exc:  # noqa: BLE001
            print(f"      下载失败：{exc}")
            continue
        downloaded.append(wheel)

        if args.download_only:
            print(f"\n  [已下载] {wheel}")
            return 0

        extra = ["--index-url", f"{BASE}/{index}"]
        if proxy:
            extra += ["--proxy", proxy]
        if pip_install(args.py, [str(wheel), *args.packages, *extra]) == 0:
            print(f"\n  [完成] PyTorch 已按 {index} 安装。")
            return 0
        print("      安装失败，换下一个源…")

    if args.download_only and downloaded:
        print(f"\n  [已下载] {downloaded[-1]}")
        return 0
    print("\n  [错误] 所有源都失败了。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
