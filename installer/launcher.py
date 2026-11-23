# -*- coding: utf-8 -*-
"""一键安装/升级启动器（PyInstaller 打包成 .exe 用）。

包里有一个 payload.zip（= 安装升级包.zip 的内容），本程序把它解到临时目录，
然后调用里面的「安装升级.bat」，由 installer\\upgrade.ps1 干活。

为什么要这么绕：7-Zip 26 的 7z.sfx 已经不带「解压完自动运行」的安装器模式了
（实测 RunProgram 不生效），所以用 PyInstaller 做一个几十行的启动器，
双击就完事 ✓
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile


def _utf8_console() -> None:
    try:
        subprocess.call("chcp 65001 >nul", shell=True)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _pause() -> None:
    if sys.stdin and sys.stdin.isatty():
        try:
            input("按回车退出…")
        except Exception:
            pass


def main() -> int:
    _utf8_console()
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.argv[0])))
    payload = os.path.join(base, "payload.zip")
    if not os.path.isfile(payload):
        print("!! 包里没有 payload.zip，这个 exe 不完整")
        _pause()
        return 2

    root = tempfile.mkdtemp(prefix="lingxi_install_")
    print("解压到：", root)
    with zipfile.ZipFile(payload) as z:
        z.extractall(root)

    bat = os.path.join(root, "安装升级.bat")
    if not os.path.isfile(bat):
        print("!! 包里没有 安装升级.bat")
        _pause()
        return 2

    # 把用户传的参数（-Target 等）原样转给 bat
    args = " ".join('"%s"' % a if " " in a else a for a in sys.argv[1:])
    cmd = 'cmd.exe /c ""%s" %s"' % (bat, args)
    rc = subprocess.call(cmd, cwd=root, shell=False)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
