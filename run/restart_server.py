r"""干净地重启本机服务（我改完代码后统一用它，别再用命令行手拼）。

为什么需要它（2026-10-17 项目主反馈"改一次就把界面关了、还多一个黑框"）：
  1. **不碰界面窗口** ✓ —— 以前的土办法会连带触发 start.bat 里的"先关旧界面"，
     或者干脆让界面失去连接；这个脚本只动服务进程 ✓
  2. **服务窗口标题正确** ✓ —— 用和 start.bat 一样的
     `start "宇之语·灵犀 服务窗口" /min cmd /c run_server.bat` ✓，
     所以你看到的就是那个正常的（最小化的）服务窗口，不会多出无标题野黑框 ✓
  3. **优雅退出**：先 POST /admin/shutdown 让服务自己收尾（落盘、关连接）✓，
     超时才强杀 ✓（强杀会留下过期 pid 文件）

用法：
    venv\Scripts\python.exe run\restart_server.py            # 默认：最小化窗口 + 优雅重启
    venv\Scripts\python.exe run\restart_server.py --visible  # 想让服务窗口显示出来
    venv\Scripts\python.exe run\restart_server.py --force    # 不等优雅退出，直接结束进程
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(errors="replace")
BASE = "http://127.0.0.1:8199"
BAT = ROOT / "app" / "run_server.bat"
PIDFILE = ROOT / "run" / "server.pid"


def health(timeout: float = 4.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:                                            # noqa: BLE001
        return None


def server_pids() -> list[int]:
    """所有在跑的 server.py 进程（三路取并集，稳妥 ✓）：
      ① `run/server.pid`（项目自己维护的）
      ② **按端口**：netstat 里 LISTENING 的那个 PID（最权威 ✓）
      ③ 命令行兜底（匹配串必须是单反斜杠 `*app\\server.py*`）
    踩过的坑：只靠 PowerShell 的命令行匹配时，转义写多了一层 →
    匹配不全 → 旧进程没停干净 → 新服务起不来（端口占用），
    health 却报着旧 PID，看着像"重启成功" ✗
    """
    pids: set[int] = set()
    try:
        if PIDFILE.exists():
            txt = PIDFILE.read_text(encoding="utf-8", errors="replace").strip()
            if txt.isdigit():
                pids.add(int(txt))
    except OSError:
        pass
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             timeout=60).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" \
                    and parts[3].upper() == "LISTENING" and parts[1].endswith(":8199"):
                pids.add(int(parts[4]))
    except Exception:                                            # noqa: BLE001
        pass
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
             "'*app" + chr(92) + "server.py*' } | Select-Object -ExpandProperty ProcessId"],
            capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
        pids.update(int(x) for x in out.split() if x.strip().isdigit())
    except Exception:                                            # noqa: BLE001
        pass
    alive: list[int] = []
    for pid in sorted(pids):
        chk = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, timeout=30).stdout.decode(
                                 "utf-8", "replace")
        if "python" in chk.lower():
            alive.append(pid)
    return alive


def kill_service_windows() -> int:
    """关掉**残留的服务窗口**（旧的 cmd 黑框）✓

    ⚠ 不能用 `taskkill /FI "WINDOWTITLE eq 宇之语·灵犀 服务窗口"`：
    中文标题受代码页影响，实测匹配不到 ✗（这正是当初写 close_ui.py 的原因）。
    改成用 PowerShell 按 MainWindowTitle 找 cmd 进程再 Stop-Process ✓
    **必须在启动新窗口之前调用** ✓
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$p = Get-Process | Where-Object { $_.MainWindowTitle -like '*服务窗口*' };"
             " $p | Stop-Process -Force -ErrorAction SilentlyContinue;"
             " ($p | Measure-Object).Count"],
            capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
        n = int(out.strip() or 0)
    except Exception:                                            # noqa: BLE001
        n = 0
    if n:
        print(f"   清掉了 {n} 个残留的服务窗口")
    return n


def port_listening() -> bool:
    """8199 还有人监听吗（这才是"服务真的停了"的判据 ✓）。

    ⚠ 别用"还有没有 server.py 进程"当判据：偶尔会留下 stale 进程
    （启动失败卡住的），它不监听端口却会让等待循环白等满 45 秒 ✗（实测）。
    """
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 8199), timeout=0.6):
            return True
    except OSError:
        return False


def stop(force: bool) -> None:
    """优雅停：先请求退出，等它自己关；超时才强杀 ✓"""
    pids = server_pids()
    if not pids:
        print("   没有在跑的服务进程")
        kill_service_windows()
        return
    print(f"   正在停止服务（PID {pids}）…")
    if not force:
        try:
            req = urllib.request.Request(f"{BASE}/admin/shutdown", method="POST")
            req.add_header("Connection", "close")
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except Exception as exc:                                 # noqa: BLE001
            print(f"   （优雅退出请求没发出去：{exc}）")
        for i in range(30):                                      # 最多等 15 秒
            time.sleep(0.5)
            if not port_listening():
                print(f"   已优雅退出（{i * 0.5:.1f}s）")
                break
        else:
            print("   优雅退出超时，强制结束")
    for pid in server_pids():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=30)
    time.sleep(1.0)
    # 停完再确认一次：端口不能还有人监听 ✓（否则新服务起不来，却看着像成功 ✗）
    if port_listening():
        left = server_pids()
        print(f"   [警告] 端口还被占着（进程 {left}），再强杀一次")
        for pid in left:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        time.sleep(1.0)
    kill_service_windows()                                       # 清掉残留的空服务窗口 ✓
    if PIDFILE.exists():
        try:
            PIDFILE.unlink()
        except OSError:
            pass


def start(minimized: bool = True) -> None:
    """用**和 start.bat 一样**的方式启动：窗口标题正确，你一眼能认出它是服务窗口 ✓"""
    # ★ 2026-10-20：起服务**之前**先把「待还原」执行掉 ✓
    #   界面上的「↩ 还原」只是登记，真正换代码/换库必须发生在服务停着的这个窗口里
    #   （服务开着时换库会被占用、换代码当前进程也用不上）
    apply_pending_restore()
    style = "/min " if minimized else ""
    # 和 start.bat 里那句完全同形（注意内层两对引号：run_server.bat 路径带空格也稳）
    cmd = f'cmd /c start "宇之语·灵犀 服务窗口" {style}cmd /c ""{BAT}""'
    subprocess.Popen(cmd, shell=True, cwd=str(ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"   已启动{'（最小化窗口）' if minimized else ''}：{BAT.name}")


def apply_pending_restore() -> None:
    """执行界面登记的「还原快照」（没有待办就什么都不做）✓"""
    try:
        sys.path.insert(0, str(ROOT))
        from app import snapshot_restore as _sr
        data = _sr.read_pending(ROOT)
        if not data:
            return
        print(f"   ↩ 发现待还原：{data.get('name')}"
              f"（代码={bool(data.get('code'))} 库={bool(data.get('db'))}）")
        res = _sr.apply_pending(ROOT, log=lambda m: print("     " + m))
        if res and res.get("errors"):
            print(f"     ⚠ 有错误：{'；'.join(res['errors'])}")
        elif res:
            print("     ✓ 还原完成（本次启动用的就是还原后的代码/库）")
    except Exception as exc:                                     # noqa: BLE001
        print(f"     ⚠ 执行待还原时出错（不影响启动，但这次没还原成功）：{exc}")


def wait_ready(seconds: int = 180) -> dict | None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        info = health()
        if info:
            print(f"   服务已就绪（{time.time() - t0:.0f}s，PID {info.get('pid')}）")
            return info
        time.sleep(2)
    return None


def ui_windows() -> int:
    """现在有几个本应用的界面窗口（复用 close_ui 的判据 ✓）"""
    try:
        r = subprocess.run([sys.executable, str(ROOT / "app" / "close_ui.py"), "--list"],
                           capture_output=True, timeout=120,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return r.stdout.decode("utf-8", "replace").count("找到窗口")
    except Exception:                                            # noqa: BLE001
        return -1


def ensure_ui() -> None:
    """没有界面窗口就打开一个。

    2026-10-17：桌面版的行为是"**手动关掉界面 → 服务才停**" ✓
    （项目主明确不要"没人用就自动关"✗）。重启服务后顺手把界面开回来，
    是为了让项目主一眼看到服务已就绪 ✓（已经有界面窗口就不重复开 ✓）。
    """
    if ui_windows() != 0:
        return
    chrome = next((p for p in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")) if p.is_file()),
        None)
    if chrome is None:
        return
    subprocess.Popen(
        [str(chrome), "--app=http://127.0.0.1:8199/search",
         f"--user-data-dir={ROOT / '.browser'}", "--window-size=1500,950",
         "--no-first-run", "--no-default-browser-check"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("   界面窗口没开着 → 顺手打开一个 ✓")


def main() -> int:
    ap = argparse.ArgumentParser(description="干净重启本机服务")
    ap.add_argument("--force", action="store_true", help="不等优雅退出，直接结束进程")
    ap.add_argument("--visible", action="store_true", help="服务窗口不要最小化")
    ap.add_argument("--no-ui", action="store_true",
                    help="不要顺手打开界面窗口（测试用：没人开界面会不会自动停）")
    args = ap.parse_args()

    print("=" * 62)
    print("  重启本机服务（不动你的界面窗口）")
    print("=" * 62)
    before = health() or {}
    stop(args.force)
    start(minimized=not args.visible)
    info = wait_ready()
    if not info:
        print("   [失败] 服务没起来，看日志：logs\\server.log")
        return 1
    if before.get("pid") and info.get("pid") == before.get("pid"):
        print(f"   [警告] 服务 PID 没变（还是 {info.get('pid')}）——"
              f"可能旧进程没停干净，新进程没起来 ✗")
        return 1
    print(f"   PID {before.get('pid') or '(之前没在跑)'} → {info.get('pid')} ✓")
    print(f"   模型 {info.get('model')} / 设备 {info.get('device')}")
    if not args.no_ui:
        ensure_ui()                                          # 界面没开就开一个 ✓
    print("   完成 ✓（界面窗口没有被关闭；刷新一下页面即可）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
