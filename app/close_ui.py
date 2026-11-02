r"""关掉「宇之语·灵犀」的界面窗口（start.bat 用浏览器 --app 模式打开的那些独立窗口）。

**怎么认定"这是本应用的窗口"**（2026-10-16 第二次修，见下面紧急止损那段）：
  · 只认 **命令行里带专用 profile** 的进程 ✓
    —— start.bat 打开界面时带的是 `--user-data-dir=<项目>\.browser`，
    所以"带这个参数的浏览器进程"= 本应用开的窗口，一个不多一个不少 ✓
  · **不再**用"标题含灵犀 + 是浏览器进程"当判据 ✗ —— 那会把用户主浏览器
    里正好开着这个界面的**整个窗口**关掉（含其它标签页）✗，实测踩过
  · 拿不到进程命令行（权限/系统不配合）时**不动手**，只报告 ✓（宁可不关也别误关）

用法：
  python app\\close_ui.py            # 关掉本应用的界面窗口（判据精确，默认就执行）
  python app\\close_ui.py --list     # 只列出来看看，不动手
  python app\\close_ui.py --wait 5   # 优雅关闭最多等 5 秒
  python app\\close_ui.py --yes      # 兼容老调用（现在默认就会关，这个参数不再必要）
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ============================================================
# 2026-10-16 紧急止损 → 同日第二次修好
#
# 第一版判据是「窗口标题含『灵犀』+ 进程属于浏览器」→ 就认成界面窗口。
# 问题是：用户的主浏览器里只要开着这个界面的标签页，**整个窗口**都会被关掉 ✗
# （Chrome 关掉一个窗口 = 那个窗口里所有标签都没了）。实测现象：
#   每次重启服务 → 用户主浏览器被关闭、必须重新打开；服务器还要重新登录。
# 于是当时改成"默认一个都不关，要关必须 --yes"。
#
# 现在判据精确了（认专用 profile 的进程 ✓），所以**恢复默认关窗** ✓：
# 只关 start.bat 自己用 `.browser` profile 开的那些窗口，用户主浏览器不受影响 ✓
# 判定拿不到命令行时仍然一个都不动 ✓（保底）。
# ============================================================
UI_PROFILE_DIR = ".browser"      # start.bat: --user-data-dir=<项目>\.browser

# 界面窗口标题里一定有这个词（search.html 的 <title>）。
# 只用来**报告**（列出找到哪些窗口），不再作为判据 ✓
TITLE_KEYS = ("灵犀", "EmbeddingGemma")
# 控制台/终端进程：界面的浏览器窗口绝不可能是它们。
SHELL_EXES = {"cmd.exe", "conhost.exe", "powershell.exe", "pwsh.exe",
              "windowsterminal.exe", "openconsole.exe"}
SKIP_TITLE_KEYS = ("服务窗口",)

WM_CLOSE = 0x0010
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000

if not sys.platform.startswith("win"):
    def main() -> int:                                     # noqa: D103
        print("[界面] 非 Windows，跳过")
        return 0
else:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM), wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindow.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    try:
        user32.SwitchToThisWindow.argtypes = [wt.HWND, wt.BOOL]
        user32.SwitchToThisWindow.restype = None
    except AttributeError:
        pass

    # 句柄是 64 位的：restype 必须显式声明成 HANDLE，否则会被截断成 int32
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                    ctypes.POINTER(wt.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    kernel32.TerminateProcess.restype = wt.BOOL
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.WaitForSingleObject.restype = wt.DWORD
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL

    def _title(hwnd) -> str:
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _pid(hwnd) -> int:
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _exe(pid: int) -> str:
        """取进程名。拿不到就返回空串（不因为没权限就把窗口漏掉）。"""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
        return ""

    def _ui_pids(root: Path) -> tuple[set[int], bool]:
        """命令行里带本应用专用 profile 的进程 PID。

        用文档里指定的 PowerShell 取法：`Get-CimInstance Win32_Process` → 命令行 ✓
        （比 wmic 靠谱，wmic 在新系统上已经没了）。
        返回 (PID 集合, 判定是否可靠)；可靠 = 至少问到了进程列表 ✓
        （问不到就返回 False，调用方据此"不动手"，宁可残留窗口也不误关主浏览器 ✗）
        """
        want = str(root / UI_PROFILE_DIR).lower()
        script = ("Get-CimInstance Win32_Process | "
                  "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        for exe in ("powershell", "pwsh"):
            try:
                out = subprocess.run(
                    [exe, "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True, timeout=30).stdout.decode(
                        "utf-8", "replace").strip()
            except (OSError, subprocess.SubprocessError):
                continue
            if not out:
                continue
            try:
                data = json.loads(out)
            except ValueError:
                continue
            if isinstance(data, dict):                            # 只有一个进程时是对象
                data = [data]
            pids = {int(r["ProcessId"]) for r in data
                    if r.get("CommandLine") and want in str(r["CommandLine"]).lower()}
            return pids, True
        return set(), False

    def find_windows(exe_allow: set[str] | None = None,
                     pids: set[int] | None = None) -> list[tuple[int, int, str]]:
        """返回 [(hwnd, pid, title)]：可见 + 属于本应用（按 pid 集合 ✓）。

        exe_allow 只在自测/兜底时用；正常情况下 pids 就是判据 ✓
        """
        allow = None if exe_allow is None else {x.lower() for x in exe_allow}
        found: list[tuple[int, int, str]] = []

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _title(hwnd)
            if any(k in title for k in SKIP_TITLE_KEYS):
                return True                     # 服务自己的黑框，别动
            pid = _pid(hwnd)
            if pids is not None:
                if pid not in pids:
                    return True                 # 不是本应用 profile 的进程 → 绝不碰 ✓
            else:
                # 兜底路径（拿不到命令行时不给用；自测时按标题 + 进程名认）
                if not title or not any(k in title for k in TITLE_KEYS):
                    return True
                exe = _exe(pid).lower()
                if exe in SHELL_EXES:
                    return True
                if allow is not None and exe and exe not in allow:
                    return True
            found.append((int(hwnd), pid, title))
            return True

        user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(callback), 0)
        return found

    def terminate(pid: int) -> bool:
        handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            ok = bool(kernel32.TerminateProcess(handle, 1))
            kernel32.WaitForSingleObject(handle, 3000)
            return ok
        finally:
            kernel32.CloseHandle(handle)

    def close_windows(wait: float = 3.0, dry: bool = False,
                      exe_allow: set[str] | None = None,
                      root: Path | None = None,
                      pids_override: set[int] | None = None) -> dict:
        """关掉本应用的界面窗口。

        pids_override 只给**自测**用（拿自己的 python 窗口当靶子，走同一套关闭代码 ✓）；
        线上调用不传，判据永远是"命令行带专用 profile 的进程" ✓
        """
        root = root or Path(__file__).resolve().parent.parent
        if pids_override is not None:
            pids, reliable = set(pids_override), True
        else:
            pids, reliable = _ui_pids(root)
        if not reliable:
            # 判定不可靠 → 一个都不动 ✓（宁可残留窗口，也不能误关用户主浏览器）
            return {"found": [], "closed": [], "killed": [], "alive": [],
                    "unreliable": True, "pids": set()}
        wins = find_windows(exe_allow=exe_allow,
                            pids=None if pids_override is not None else pids)
        if dry or not wins:
            return {"found": wins, "closed": [], "killed": [], "alive": [],
                    "unreliable": False, "pids": pids}

        for hwnd, _pid, _title in wins:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

        deadline = time.time() + max(0.0, wait)
        alive = wins
        while time.time() < deadline:
            alive = [w for w in wins if user32.IsWindow(w[0])]
            if not alive:
                break
            time.sleep(0.2)

        # 还活着的：同一个浏览器进程可能挂着好几个窗口，按 PID 去重再杀
        killed: list[int] = []
        for pid in {w[1] for w in alive}:
            if terminate(pid):
                killed.append(pid)
        time.sleep(0.3)
        still = [w for w in wins if user32.IsWindow(w[0])]
        return {"found": wins, "closed": [w for w in wins if w not in alive],
                "killed": killed, "alive": still, "unreliable": False, "pids": pids}

    def focus_existing() -> tuple[int, bool]:
        """已经有本应用的界面窗口就**只把它调到前面**，一个都不关 ✓。

        给 `start.bat` 用：以前它每次都"先关旧窗口再开新的" ✗ ——
        项目主重启服务后运行 start.bat，正在看的界面就被关掉重开了 ✗
        （2026-10-17 反馈）。现在有现成的就聚焦它 ✓，没有才新开 ✓。
        返回 (窗口数, 是否找到了可聚焦的窗口)。
        """
        pids, _reliable = _ui_pids(Path(__file__).resolve().parent.parent)
        wins = find_windows(pids=pids)
        if not wins:
            return 0, False
        hwnd = wins[0][0]
        try:
            user32.ShowWindow(hwnd, SW_RESTORE)          # 最小化了就先还原
            user32.SetForegroundWindow(hwnd)             # 再调到前台
            if hasattr(user32, "SwitchToThisWindow"):
                user32.SwitchToThisWindow(hwnd, True)    # 前台锁的常见绕过 ✓
        except Exception:                                # noqa: BLE001
            pass                                         # 聚焦失败没关系，反正不关它 ✓
        return len(wins), True

    def main() -> int:
        parser = argparse.ArgumentParser(description="关掉「宇之语·灵犀」的界面窗口")
        parser.add_argument("--list", action="store_true", help="只列出窗口，不关闭")
        parser.add_argument("--wait", type=float, default=3.0, help="优雅关闭最多等几秒")
        parser.add_argument("--reuse", action="store_true",
                            help="已经有界面窗口就只激活、不关闭（start.bat 用）；"
                                 "退出码 0=已经有并已激活，1=没有")
        parser.add_argument("--yes", action="store_true",
                            help="兼容老调用（判据精确后默认就会关，这个参数不再必要）")
        args = parser.parse_args()

        try:
            sys.stdout.reconfigure(errors="replace")       # 控制台是 GBK，别因编码崩掉
        except Exception:                                  # noqa: BLE001
            pass

        if args.reuse:
            n, ok = focus_existing()
            if ok:
                print(f"[界面] 已经有 {n} 个界面窗口，已把它调到前面（不重开 ✓）")
                return 0
            print("[界面] 还没有界面窗口")
            return 1

        result = close_windows(wait=args.wait, dry=args.list)
        if result.get("unreliable"):
            print("[界面] 拿不到进程命令行（权限不足？）→ 一个窗口都没动 ✓")
            print("       这样做是为了绝不误关你的主浏览器；要关请手动关那个独立窗口。")
            return 0
        found = result["found"]
        print(f"[界面] 本应用专用配置（.{UI_PROFILE_DIR}）的进程 {len(result['pids'])} 个，"
              f"其中可见窗口 {len(found)} 个")
        if not found:
            print("[界面] 没有找到开着的界面窗口")
            return 0
        if args.list:
            for _hwnd, pid, title in found:
                print(f"[界面] 找到窗口 pid={pid} 标题={title}")
            return 0

        print(f"[界面] 关掉 {len(result['closed'])} 个界面窗口"
              + (f"，强制结束 {len(result['killed'])} 个进程" if result["killed"] else ""))
        for _hwnd, pid, title in result["alive"]:
            print(f"[界面] 还有一个没关掉：pid={pid} 标题={title}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
