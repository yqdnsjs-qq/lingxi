"""索引库的备份与恢复。

为什么不能直接复制 index.db：
    数据库跑在 WAL 模式下，最近的改动可能还在 index.db-wal 里没合并。
    单独拷 index.db 会丢掉这部分数据（旧扫描结果看起来"少了"）。
    这里用 SQLite 官方的在线备份接口做一致性快照，服务开着也能安全备份。

要备份的只有两样东西，加起来不到 10 MB：
    data\\index\\index.db   索引库（文件夹配置也在这里面）
    config.ini              全局设置
不需要备份的：models\\ 和 .cache\\（模型，几 GB，可以重新下载）、venv\\（依赖）
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.config import Config
else:
    from .config import Config

KEEP_BACKUPS = 20


def _describe(db_path: Path, immutable: bool = False) -> dict:
    """读一个索引库，返回它的规模和文件夹列表。

    immutable=1 用于读备份文件：告诉 SQLite 这个文件不会变，
    它就不会在旁边生成 -wal / -shm 尾巴，备份永远是干净的单文件。
    """
    params = "mode=ro&immutable=1" if immutable else "mode=ro"
    conn = sqlite3.connect(f"file:{db_path}?{params}", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        info = {
            "folders": conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0],
            "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "errors": conn.execute(
                "SELECT COUNT(*) FROM files WHERE status='error'").fetchone()[0],
            "paths": [r["path"] for r in conn.execute(
                "SELECT path FROM folders ORDER BY added_at")],
        }
    except sqlite3.DatabaseError as exc:
        info = {"broken": str(exc)}
    finally:
        conn.close()
    return info


def make_backup(cfg: Config, note: str = "") -> Path:
    src_path = cfg.db_path
    if not src_path.exists():
        raise SystemExit(f"[错误] 找不到索引库：{src_path}\n       还没有建立过索引，不需要备份。")

    dest_dir = cfg.root / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"index-{stamp}.db"

    # 在线备份：source.backup(target) 会做一致性快照，服务运行中也安全
    source = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
        # 存成 DELETE 模式：备份就是干干净净一个 .db 文件，不带 WAL 尾巴。
        # 恢复后程序打开时会自动切回 WAL，所以没有副作用。
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        target.close()
        source.close()
    for suffix in ("-wal", "-shm"):
        stray = dest.with_name(dest.name + suffix)
        if stray.exists():
            stray.unlink()

    if (cfg.root / "config.ini").exists():
        shutil.copy2(cfg.root / "config.ini", dest.with_name(dest.stem + ".ini"))

    info = _describe(dest, immutable=True)
    manifest = dest.with_name(dest.stem + ".txt")
    lines = [
        f"备份时间 : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"来源     : {src_path}",
        f"大小     : {dest.stat().st_size / 1e6:.2f} MB",
        f"文件夹   : {info.get('folders', '?')} 个",
        f"文件     : {info.get('files', '?')} 条记录（异常 {info.get('errors', '?')}）",
        f"片段     : {info.get('chunks', '?')} 个",
        "",
        "包含的文件夹：",
    ]
    lines += [f"  - {p}" for p in info.get("paths", [])] or ["  （无）"]
    if note:
        lines += ["", f"备注：{note}"]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _prune(dest_dir)
    return dest


def _prune(dest_dir: Path) -> None:
    """只保留最近 KEEP_BACKUPS 份，避免越攒越多。"""
    backups = sorted(dest_dir.glob("index-*.db"))
    for old in backups[:-KEEP_BACKUPS]:
        for extra in (old, old.with_name(old.stem + ".ini"),
                      old.with_name(old.stem + ".txt"),
                      old.with_name(old.name + "-wal"),
                      old.with_name(old.name + "-shm")):
            try:
                extra.unlink()
            except OSError:
                pass


def list_backups(cfg: Config) -> list[Path]:
    dest_dir = cfg.root / "backups"
    if not dest_dir.exists():
        return []
    return sorted(dest_dir.glob("index-*.db"), reverse=True)


def restore(cfg: Config, name: str, target_db: Path | None = None) -> Path:
    """把某份备份恢复成当前索引库。会先把现有库另存一份，避免误操作。"""
    dest_dir = cfg.root / "backups"
    source = Path(name)
    if not source.is_absolute():
        source = dest_dir / name
    if source.suffix != ".db":
        source = source.with_suffix(".db")
    if not source.exists():
        raise SystemExit(f"[错误] 找不到备份文件：{source}")

    target = target_db or cfg.db_path
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safety = target.parent / f"index-before-restore-{stamp}.db"
        shutil.copy2(target, safety)
        print(f"  现有索引已另存为：{safety.name}")

    # WAL/SHM 是旧库的残留，必须清掉，否则会被当成新库的日志
    for suffix in ("-wal", "-shm"):
        leftover = target.with_name(target.name + suffix)
        if leftover.exists():
            leftover.unlink()

    shutil.copy2(source, target)

    backup_ini = source.with_name(source.stem + ".ini")
    if backup_ini.exists():
        shutil.copy2(backup_ini, cfg.root / "config.ini")
        print("  config.ini 也已一并恢复")
    return target



# --------------------------------------------------------------- 图形界面
def _gui_ready() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _gui_root():
    """建一个隐藏的根窗口并强制置顶，避免对话框跑到后台去。"""
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update()
    except Exception:  # noqa: BLE001
        pass
    return root


def gui_pick_backup(cfg: Config) -> Path | None:
    """弹出文件选择框，默认定位到 backups 目录。"""
    from tkinter import filedialog

    folder = cfg.root / "backups"
    root = _gui_root()
    try:
        chosen = filedialog.askopenfilename(
            parent=root,
            title="选择要恢复的索引备份",
            initialdir=str(folder if folder.exists() else cfg.root),
            filetypes=[("索引备份 (*.db)", "*.db"), ("所有文件", "*.*")],
        )
    finally:
        root.destroy()
    return Path(chosen) if chosen else None


def gui_ask(title: str, message: str) -> bool:
    from tkinter import messagebox

    root = _gui_root()
    try:
        return bool(messagebox.askyesno(title, message, parent=root))
    finally:
        root.destroy()


def gui_info(title: str, message: str) -> None:
    from tkinter import messagebox

    root = _gui_root()
    try:
        messagebox.showinfo(title, message, parent=root)
    finally:
        root.destroy()


def gui_error(title: str, message: str) -> None:
    from tkinter import messagebox

    root = _gui_root()
    try:
        messagebox.showerror(title, message, parent=root)
    finally:
        root.destroy()


# --------------------------------------------------------------- 服务控制
def service_running(cfg: Config) -> bool:
    check_host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "::0") else cfg.host
    try:
        with urllib.request.urlopen(f"http://{check_host}:{cfg.port}/health", timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _run_bat(cfg: Config, name: str, wait: bool, new_console: bool = False) -> None:
    bat = cfg.root / name
    if not bat.exists():
        return
    # stdin 给空，避免脚本里的 pause 卡住自动流程
    if wait:
        subprocess.run(["cmd", "/c", str(bat)], cwd=str(cfg.root),
                       stdin=subprocess.DEVNULL, timeout=180)
    else:
        flags = 0
        if new_console and hasattr(subprocess, "CREATE_NEW_CONSOLE"):
            flags = subprocess.CREATE_NEW_CONSOLE      # 给它一个自己的窗口
        subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(cfg.root),
                         stdin=subprocess.DEVNULL, creationflags=flags)


def summarize(path: Path) -> str:
    info = _describe(path, immutable=True)
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    if "broken" in info:
        return f"这个文件不是有效的索引备份：\n{info['broken'][:200]}"
    lines = [
        f"文件：{path.name}",
        f"时间：{when}",
        f"大小：{path.stat().st_size / 1e6:.2f} MB",
        "",
        f"文件夹：{info['folders']} 个",
        f"文件记录：{info['files']} 条（异常 {info['errors']}）",
        f"向量片段：{info['chunks']} 个",
        "",
        "包含的文件夹：",
    ]
    lines += [f"  · {p}" for p in info["paths"]] or ["  （无）"]
    return "\n".join(lines)


def restore_interactive(cfg: Config) -> int:
    """图形化恢复流程：选文件 → 确认 → 自动停服务 → 恢复 → 问是否重启。"""
    print("=" * 60)
    print("  EmbeddingGemma 2 - 恢复索引库")
    print("=" * 60)

    if not _gui_ready():
        return _restore_by_console(cfg)

    chosen = gui_pick_backup(cfg)
    if chosen is None:
        print("  已取消，没有做任何改动。")
        return 0

    if chosen.suffix.lower() != ".db":
        gui_error("无法恢复", "请选择 .db 的索引备份文件。")
        return 1

    summary = summarize(chosen)
    if "不是有效的索引备份" in summary:
        gui_error("无法恢复", summary)
        return 1
    print("  已选择：", chosen)
    print(summary.replace("\n", "\n  "))
    print()

    if not gui_ask("确认恢复", f"{summary}\n\n恢复会覆盖当前的索引，确定继续吗？"):
        print("  已取消，没有做任何改动。")
        return 0

    was_running = service_running(cfg)
    if was_running:
        if gui_ask("服务正在运行", "恢复索引需要先停止服务。\n现在自动停止并继续吗？"):
            print("  正在停止服务…")
            _run_bat(cfg, "stop.bat", wait=True)
            for _ in range(10):
                if not service_running(cfg):
                    break
                time.sleep(1)
        else:
            print("  已取消。请先双击 stop.bat 再试。")
            return 0

    try:
        target = restore(cfg, str(chosen))
    except SystemExit as exc:
        gui_error("恢复失败", str(exc))
        return 1

    info = _describe(target, immutable=True)
    done = (f"恢复完成！\n\n"
            f"{info['folders']} 个文件夹 · {info['files']} 条文件记录 · {info['chunks']} 个片段\n\n"
            f"旧的索引已备份为 index-before-restore-*.db（在 data\\index 里）")
    print("  " + done.replace("\n", "\n  "))
    gui_info("恢复完成", done)

    if gui_ask("启动服务", "现在启动服务吗？\n（会打开一个新窗口，模型加载约 10-60 秒）"):
        print("  正在启动服务…（在新窗口里，模型加载约 10-60 秒）")
        _run_bat(cfg, "search.bat", wait=False, new_console=True)
    else:
        print("  好的。需要时双击 search.bat 启动。")
    return 0


def _restore_by_console(cfg: Config) -> int:
    """没有图形界面时的退路：列出编号，输入序号即可。"""
    items = list_backups(cfg)
    if not items:
        print("  还没有任何备份。")
        return 1
    print("  选择要恢复的备份（直接输入序号）：")
    for index, path in enumerate(items, 1):
        info = _describe(path, immutable=True)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        print(f"    {index:2d}) {when}  {path.name}  "
              f"{info.get('files', '?')} 文件 / {info.get('chunks', '?')} 片段")
    print()
    try:
        choice = input("  序号（回车取消）: ").strip()
    except EOFError:
        return 0
    if not choice.isdigit() or not 1 <= int(choice) <= len(items):
        print("  已取消。")
        return 0
    return restore_console_confirm(cfg, items[int(choice) - 1])


def restore_console_confirm(cfg: Config, path: Path) -> int:
    print()
    print("  " + summarize(path).replace("\n", "\n  "))
    print()
    if service_running(cfg):
        print("  [提醒] 服务正在运行，请先双击 stop.bat 再恢复。")
        return 1
    if restore(cfg, str(path)):
        print()
        print("  恢复完成。双击 search.bat 启动服务即可。")
        return 0
    return 1

def main(argv: list[str]) -> int:
    cfg = Config()
    action = (argv[0] if argv else "backup").lower()

    if action in ("backup", "make", ""):
        print("=" * 60)
        print("  EmbeddingGemma 2 - 备份索引库")
        print("=" * 60)
        dest = make_backup(cfg, note=" ".join(argv[1:]))
        info = _describe(dest, immutable=True)
        print(f"  已备份到 : {dest}")
        print(f"  大小     : {dest.stat().st_size / 1e6:.2f} MB")
        print(f"  内容     : {info.get('folders')} 个文件夹 · "
              f"{info.get('files')} 条文件记录 · {info.get('chunks')} 个片段")
        print(f"  设置     : {dest.with_name(dest.stem + '.ini').name}")
        print(f"  说明     : {dest.with_name(dest.stem + '.txt').name}")
        print()
        print(f"  备份目录 : {cfg.root / 'backups'}")
        print(f"  （自动只保留最近 {KEEP_BACKUPS} 份）")
        return 0

    if action == "list":
        items = list_backups(cfg)
        print("=" * 60)
        print("  EmbeddingGemma 2 - 已有备份")
        print("=" * 60)
        if not items:
            print("  还没有任何备份。双击 backup.bat 立刻做一份。")
            return 0
        for path in items:
            info = _describe(path, immutable=True)
            when = time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(path.stat().st_mtime))
            print(f"  {path.name}")
            print(f"      {when} · {path.stat().st_size / 1e6:.2f} MB · "
                  f"{info.get('folders', '?')} 个文件夹 · "
                  f"{info.get('files', '?')} 条记录 · {info.get('chunks', '?')} 个片段")
        print()
        print("  恢复用法：backup.bat restore <文件名>")
        return 0

    if action == "restore":
        if len(argv) < 2:
            # 不敲键盘：直接弹文件选择框
            return restore_interactive(cfg)
        # 带了文件名就走命令行（给脚本/自动化用）
        print("=" * 60)
        print("  EmbeddingGemma 2 - 恢复索引库")
        print("=" * 60)
        if service_running(cfg):
            print("  [错误] 服务正在运行，请先双击 stop.bat 再恢复。")
            return 1
        target = restore(cfg, argv[1])
        info = _describe(target, immutable=True)
        print(f"  已恢复到 : {target}")
        print(f"  内容     : {info.get('folders')} 个文件夹 · "
              f"{info.get('files')} 条记录 · {info.get('chunks')} 个片段")
        print()
        print("  接下来双击 search.bat 启动服务即可。")
        return 0

    print("用法：")
    print("  backup.bat                  立刻备份一份")
    print("  backup.bat list             列出已有备份")
    print("  backup.bat restore <文件>   从备份恢复")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
