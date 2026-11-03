"""桌面交互：定位文件、打开文件，以及失败时的自我诊断。

为什么要这么麻烦：
- 直接 `explorer /select,` 新起 explorer.exe，在某些环境会以 0xC0000142
  （STATUS_DLL_INIT_FAILED）启动失败，用户只看到一个报错弹窗；
- Windows 的 UIPI 机制还把进程分成完整性级别，**低完整性进程根本不允许
  操作普通资源管理器窗口**（被沙箱/受限工具启动的服务就是这种情况），
  连 Shell 接口都会返回 E_ACCESSDENIED。

所以这里优先走 Shell 接口让「已经在运行的资源管理器」干活，不新起进程，
并且在失败时把「为什么失败、该怎么办」讲清楚。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("eg2.desktop")

_IS_WINDOWS = sys.platform == "win32"

# 完整性级别 RID
_INTEGRITY = {
    0x0000: "Untrusted",
    0x1000: "Low",
    0x2000: "Medium",
    0x2100: "Medium Plus",
    0x3000: "High",
    0x4000: "System",
    0x5000: "Protected",
}


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


def is_elevated() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def integrity_level() -> str:
    """当前进程的完整性级别：Low / Medium / High / …（失败返回「未知」）。"""
    if not _IS_WINDOWS:
        return "未知"
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # GetCurrentProcess 返回的是伪句柄，不设 restype 会被截断成 32 位而失效
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                         ctypes.byref(token)):
            return "未知"
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            if not size.value:
                return "未知"
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 25, buffer, size.value,
                                                ctypes.byref(size)):
                return "未知"
            label = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
            count = advapi32.GetSidSubAuthorityCount(label.Label.Sid)
            if not count:
                return "未知"
            rid = advapi32.GetSidSubAuthority(label.Label.Sid, count[0] - 1)[0]
            return _INTEGRITY.get(rid, f"0x{rid:X}")
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001
        return "未知"


def shell_running() -> bool:
    """会话里有没有在跑的资源管理器（用任务栏窗口判断）。"""
    if not _IS_WINDOWS:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        return bool(user32.FindWindowW("Shell_TrayWnd", None))
    except Exception:  # noqa: BLE001
        return False


def _enum_folder_windows() -> list[tuple[int, str]]:
    """列出所有资源管理器文件夹窗口 (HWND, 标题)。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]

    found: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value not in ("CabinetWClass", "ExploreWClass"):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(hwnd, title, length + 2)
        found.append((int(hwnd), title.value))
        return True

    try:
        user32.EnumWindows(callback_type(visit), 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("枚举资源管理器窗口失败：%s", exc)
    return found


def _activate(hwnd: int) -> bool:
    """把窗口提到最前。Windows 默认不允许后台程序抢焦点，所以要多试几招。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE

    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True

    # 招数一：把当前前台线程的输入队列临时接过来，这样就有资格设置前台窗口
    foreground = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(foreground, None)
    my_thread = kernel32.GetCurrentThreadId()
    if fg_thread and fg_thread != my_thread:
        user32.AttachThreadInput(my_thread, fg_thread, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(my_thread, fg_thread, False)
    if user32.GetForegroundWindow() == hwnd:
        return True

    # 招数二：模拟一次 ALT 按键，Windows 会认为「用户刚操作过」，从而解除前台锁定
    try:
        user32.keybd_event(0x12, 0, 0, 0)            # VK_MENU down
        user32.keybd_event(0x12, 0, 2, 0)            # VK_MENU up
    except Exception:  # noqa: BLE001
        pass
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True

    # 招数三：焦点实在抢不到，至少把窗口抬到最上层，让用户能直接看见，
    # 而不是只在任务栏闪一下
    try:
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        flags = 0x0001 | 0x0002 | 0x0040          # NOSIZE | NOMOVE | SHOWWINDOW
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)     # HWND_TOPMOST
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)     # HWND_NOTOPMOST
        log.info("无法抢占焦点，已把窗口抬到最上层")
        return True
    except Exception:  # noqa: BLE001
        return False


_TITLE_SUFFIXES = (
    " - 文件资源管理器",
    " - File Explorer",
    " - Windows 资源管理器",
    " - Windows Explorer",
)


def _clean_window_title(title: str) -> str:
    """Win11 的窗口标题是「文件夹名 - 文件资源管理器」，把后缀去掉再比。"""
    cleaned = title.strip()
    for suffix in _TITLE_SUFFIXES:
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)].strip()
    return cleaned


def bring_folder_to_front(folder_name: str, timeout: float = 3.0) -> bool:
    """把显示这个文件夹的资源管理器窗口提到最前面。

    后台进程打开的资源管理器只会在任务栏闪一下（Windows 的前台锁定），
    这里等窗口出现后主动激活它。
    """
    if not _IS_WINDOWS or not folder_name:
        return False

    deadline = time.time() + timeout
    target = folder_name.strip().lower()
    while True:
        windows = [(_clean_window_title(title).lower(), hwnd, title)
                   for hwnd, title in _enum_folder_windows()]
        # 先找标题完全一致的，再退化成包含匹配，避免误激活别的窗口
        for matcher in (lambda name: name == target, lambda name: target in name):
            for name, hwnd, title in windows:
                if matcher(name) and _activate(hwnd):
                    log.info("已把资源管理器窗口提到最前：%s", title)
                    return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def start_shell() -> bool:
    """把资源管理器拉起来（有些会话里它没在运行，定位就会一直失败）。"""
    if not _IS_WINDOWS:
        return False
    try:
        subprocess.Popen("explorer.exe",
                         cwd=os.environ.get("SystemRoot", "C:\\"), shell=False)
    except OSError as exc:
        log.warning("启动资源管理器失败：%s", exc)
        return False
    for _ in range(12):                    # 最多等 6 秒
        if shell_running():
            log.info("已把资源管理器启动起来")
            return True
        time.sleep(0.5)
    return False


def reveal_in_explorer(target: str) -> str:
    """让已经运行的资源管理器选中这个文件，并把窗口提到最前。成功返回空字符串。"""
    if not _IS_WINDOWS:
        return "当前系统不支持"

    # 尽力告诉系统「接下来允许别的进程抢前台」，缓解窗口只在任务栏闪的问题
    try:
        ctypes.WinDLL("user32").AllowSetForegroundWindow(-1)     # ASFW_ANY
    except Exception:  # noqa: BLE001
        pass

    problem = _com_select(target)
    if not problem:
        # 窗口已经打开，但多半只闪了一下任务栏 —— 主动把它提到最前
        bring_folder_to_front(Path(target).parent.name, timeout=2.5)
        return ""

    # E_ACCESSDENIED 多半是因为会话里压根没有资源管理器在跑：
    # 先把它拉起来，再试一次，能把「永远失败」变成「稍等一下就好」
    if problem.endswith("0x80070005") and not shell_running():
        log.info("会话里没有资源管理器，先启动它再重试定位…")
        if start_shell():
            retry = _com_select(target)
            if not retry:
                bring_folder_to_front(Path(target).parent.name, timeout=3.0)
                return ""
            problem = retry
    return problem


def _com_select(target: str) -> str:
    """调用 SHOpenFolderAndSelectItems，成功返回空字符串。"""
    box: dict[str, str] = {}

    def worker() -> None:
        try:
            ole32 = ctypes.WinDLL("ole32")
            shell32 = ctypes.WinDLL("shell32")
            shell32.SHParseDisplayName.argtypes = [
                wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            shell32.SHParseDisplayName.restype = ctypes.c_long
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long

            hr = ole32.CoInitializeEx(None, 0x2)       # APARTMENTTHREADED
            inited = hr in (0, 1)
            try:
                pidl = ctypes.c_void_p()
                attrs = wintypes.DWORD()
                hr = shell32.SHParseDisplayName(target, None, ctypes.byref(pidl), 0,
                                                ctypes.byref(attrs))
                if hr != 0:
                    box["err"] = f"SHParseDisplayName=0x{hr & 0xFFFFFFFF:08X}"
                    return
                try:
                    hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
                    if hr != 0:
                        box["err"] = (f"SHOpenFolderAndSelectItems="
                                      f"0x{hr & 0xFFFFFFFF:08X}")
                finally:
                    ole32.CoTaskMemFree(pidl)
            finally:
                if inited:
                    ole32.CoUninitialize()
        except Exception as exc:  # noqa: BLE001
            box["err"] = str(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(10)
    if thread.is_alive():
        return "Shell 调用超时"
    return box.get("err", "")


_DIAG_CACHE: tuple[float, dict] | None = None


def diagnose() -> dict:
    """把影响桌面交互的环境因素都列出来，方便排错。

    ★ 2026-11-03：加 60 秒缓存。`/health` 每次都调它，而它要反复
      `ctypes.WinDLL(...)` 加载系统库；索引跑满时这些调用会把健康检查拖到超时 ✗
      这些信息（完整性级别、资源管理器在不在）一分钟内不会变 ✓
    """
    global _DIAG_CACHE
    now = time.time()
    if _DIAG_CACHE is not None and now - _DIAG_CACHE[0] < 60:
        return dict(_DIAG_CACHE[1])

    level = integrity_level()
    shell = shell_running() if _IS_WINDOWS else True
    out = {
        "platform": sys.platform,
        "elevated": is_elevated(),
        "integrity": level,
        "shell_running": shell,
        "desktop_ok": (not _IS_WINDOWS) or (
            level in ("Medium", "Medium Plus", "High") and shell),
        "session": os.environ.get("SESSIONNAME", ""),
    }
    _DIAG_CACHE = (now, dict(out))
    return out


def failure_hint(problem: str) -> str:
    """把失败原因翻译成用户能照着做的说明。"""
    info = diagnose()
    level = info["integrity"]
    lines = [f"打不开资源管理器（{problem}）。"]

    if _IS_WINDOWS and not info["shell_running"]:
        lines.append(
            "你的 Windows 会话里没有运行「资源管理器」(explorer.exe)，"
            "所以没有窗口可以显示文件位置。")
        lines.append("按 Ctrl+Shift+Esc 打开任务管理器 →「文件」→「运行新任务」→ "
                     "输入 explorer.exe 回车，把它启动起来，再点一次「定位文件」即可。")
    elif _IS_WINDOWS and level not in ("Medium", "Medium Plus", "High", "未知"):
        lines.append(
            f"当前服务进程的完整性级别是 {level} —— Windows 不允许低完整性进程"
            f"操作普通资源管理器窗口，所以定位/打开都会失败。")
        lines.append("这通常意味着服务是被沙箱、计划任务或其它受限工具启动的。"
                     "请双击 stop.bat 关掉，再在桌面上双击 search.bat 重新启动。")
    elif info["elevated"]:
        lines.append(
            "当前服务是以管理员身份运行的。管理员进程和普通资源管理器之间"
            "受 Windows 的 UIPI 隔离，定位常常会失败。")
        lines.append("建议不要用「以管理员身份运行」启动 search.bat。")
    else:
        lines.append("可能是资源管理器没有正常响应。可以试试："
                     "在任务管理器里重启「Windows 资源管理器」。")

    lines.append("临时可以点「预览」看内容，或点「复制路径」粘到资源管理器地址栏，"
                 "也可以点「下载到本机」把文件存下来。")
    return " ".join(lines)


def open_with_default(target: str) -> str:
    """用系统默认程序打开文件。"""
    if not _IS_WINDOWS:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])
        return "已打开"
    try:
        os.startfile(target)  # noqa: S606
        return "已用默认程序打开"
    except OSError:
        subprocess.Popen(f'cmd /c start "" "{target}"', shell=False)
        return "已尝试用默认程序打开"


def open_in_file_manager(path: Path) -> str:
    target = os.path.normpath(str(path))
    if _IS_WINDOWS:
        subprocess.Popen(f'explorer /select,"{target}"',
                         cwd=os.environ.get("SystemRoot", "C:\\"))
        return "已尝试在资源管理器中定位"
    subprocess.Popen(["xdg-open", str(path.parent)])
    return "已打开所在目录"
