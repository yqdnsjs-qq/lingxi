"""修复后的说明与自检（由 fix-permissions.bat 在最后调用）。

中文说明放在 .py 里而不是 .bat 里：中文 Windows 的代码页通常是 936，
UTF-8 中文写在批处理里会让 cmd.exe 解析错位、把注释当命令执行。
"""
from __future__ import annotations

# ---- Windows GBK 控制台保护 ----
# 这个脚本会打印 ✓ ✗ ⚠ 这类字符，cp936 编不出来会直接 UnicodeEncodeError 崩掉。
# 改成"编不出就降级成 ?"，中文照常显示。
import sys as _sys

try:
    _sys.stdout.reconfigure(errors="replace")
    _sys.stderr.reconfigure(errors="replace")
except Exception:                                          # noqa: BLE001
    pass
# ---- 保护结束 ----

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def label_of(target: Path) -> str:
    try:
        out = subprocess.run(["icacls", str(target)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30).stdout
    except Exception as exc:  # noqa: BLE001
        return f"（查询失败：{exc}）"
    for line in out.splitlines():
        if "mandatory" in line.lower():
            return line.strip()
    return "（无标签 = 标准 Medium）"


def main() -> int:
    print("=" * 60)
    print("  修复结果与下一步")
    print("=" * 60)
    print(f"  项目目录 : {label_of(ROOT)}")
    print(f"  python   : {label_of(ROOT / 'venv' / 'Scripts' / 'python.exe')}")
    print()

    py_label = label_of(ROOT / "venv" / "Scripts" / "python.exe")
    if "low" in py_label.lower():
        print("  ✗ python.exe 仍然是 Low —— 修复没有生效。")
        print("    可能是 UAC 被取消了，或者安全软件拦住了 icacls。")
        print("    请右键 fix-permissions.bat →「以管理员身份运行」再试一次。")
    else:
        print("  ✓ python.exe 已不再是 Low 完整性，这一步成功了。")
        print()
        print("  接下来请依次做：")
        print("    1. 双击 stop.bat      （关掉正在运行的服务）")
        print("    2. 双击 search.bat    （用新权限重新启动）")
        print("    3. 双击 status.bat    （确认结果）")
        print()
        print("  status.bat 里应该看到：")
        print("      桌面交互 : 正常 —— 「定位文件 / 用默认程序打开」可用")
        print()
        print("  如果仍是「不可用」，说明当前会话权限受限，")
        print("  那就继续用「预览」「复制路径」「下载到本机」这三条路。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
