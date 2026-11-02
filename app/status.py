"""给人看的服务状态报告（由 status.bat 调用）。"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.config import Config
else:
    from .config import Config


def fetch(url: str, timeout: float = 6.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return {"_error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"_error": str(exc)}


def describe_desktop(info: dict) -> list[str]:
    """把「定位文件能不能用」讲成正常人能看懂的话。"""
    if not info:
        return ["  桌面交互 : 未知（服务没返回这项信息，可能是旧版本）"]

    if info.get("desktop_ok"):
        return ["  桌面交互 : 正常 —— 「定位文件 / 用默认程序打开」可用"]

    level = info.get("integrity", "未知")
    lines = [f"  桌面交互 : 不可用 —— 「定位文件 / 用默认程序打开」会失败"]
    if not info.get("shell_running", True):
        lines.append("             原因：当前 Windows 会话里没有运行资源管理器")
        lines.append("             办法：任务管理器 →「文件」→「运行新任务」→ 输入 explorer.exe")
    elif level not in ("Medium", "Medium Plus", "High", "未知"):
        lines.append(f"             原因：服务进程的完整性级别是 {level}，"
                     "Windows 不允许受限进程操作资源管理器")
        lines.append("             办法：别从沙箱/后台工具里启动，"
                     "直接在桌面双击 search.bat")
    elif info.get("elevated"):
        lines.append("             原因：服务以管理员身份运行，"
                     "管理员进程和普通资源管理器之间受 Windows 隔离")
        lines.append("             办法：不要用「以管理员身份运行」启动 search.bat")
    else:
        lines.append("             原因：资源管理器没有正常响应")
        lines.append("             办法：在任务管理器里重启「Windows 资源管理器」")
    lines.append("             仍然可用：预览、复制路径、下载到本机")
    return lines


def main() -> int:
    cfg = Config()
    # 服务如果监听 0.0.0.0，健康检查要走回环地址
    check_host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "::0") else cfg.host
    base = f"http://{check_host}:{cfg.port}"
    health = fetch(f"{base}/health")

    print("=" * 60)
    print("  EmbeddingGemma 2 本地服务 - 状态")
    print("=" * 60)
    print(f"  模型目录 : {cfg.model_dir}")
    print(f"  服务地址 : {base}/")

    if health.get("_error"):
        print("  进程 PID : 未运行")
        print()
        print(f"[状态] 服务未运行（{health['_error']}）。双击 start.bat 启动。")
        return 1

    print(f"  进程 PID : {health.get('pid')}")
    print(f"  已运行   : {int(health.get('uptime_seconds', 0))} 秒")
    print(f"  运行设备 : {health.get('device')} / {health.get('dtype')}")
    print(f"  模态     : {health.get('modality')}（配置为 {health.get('configured_modality')}）")
    print(f"  向量维度 : {health.get('dimension')}（最长 {health.get('max_length')} token）")
    if health.get("reloading"):
        print(f"  模型状态 : 正在重新加载… {health.get('message', '')}")
    print()
    for line in describe_desktop(health.get("desktop") or {}):
        print(line)
    print()

    folders = fetch(f"{base}/api/folders")
    if not folders.get("_error"):
        stats = folders.get("stats", {})
        print(f"  索引情况 : {len(folders.get('folders', []))} 个文件夹 · "
              f"{stats.get('files', 0)} 个文件 · {stats.get('chunks', 0)} 个片段"
              f" · 异常 {stats.get('errors', 0)}")
    print()
    print("[状态] 服务运行中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
