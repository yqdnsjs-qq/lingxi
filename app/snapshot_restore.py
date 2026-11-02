"""快照还原：把项目退回到某一份快照的状态（本机 / 服务器通用）。

为什么要"登记 + 启动前执行"，而不是"点一下就换"
------------------------------------------------
换代码和换索引库都**必须在服务停着的时候做**：
  · 索引库被服务打开着，Windows 上覆盖不了；换了半截还会让服务读到坏库 ✗
  · 代码换了但进程已经加载过模块，当前进程仍在跑旧代码 ✗

所以流程是：
  ① 界面点「还原」→ 只写一个 pending 文件（`backups/pending-restore-snapshot.json`）
  ② 服务下次**启动之前**，`apply_pending()` 把它执行掉
     本机：由 run\\restart_server.py 在起服务前调用
     服务器：由 systemd 的 ExecStartPre 调用（见 run\\deploy\\install_restore_prestart.sh）
  ③ 执行时**先把现有库改名留底**（index-before-restore-<时间>.db），再覆盖 ✓

还原范围（可分开选）：
  · db   = 索引库（数据回到那一刻）
  · code = 只换 `app\\`（源码 + 静态页）；**不动 config.ini / run / docs**
           —— 配置里有你的密钥和路径，退代码不该把它们一起退回去 ✗
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

PENDING_NAME = "pending-restore-snapshot.json"

# 快照里索引库可能在两个位置（两种快照脚本的布局不一样）：
#   本机 run\snapshot.py      →  <快照>\data\index\index.db（整棵目录树都拷）
#   服务器 snapshot_server.sh →  <快照>\index.db            （只挑要的几样东西）
# ⚠ 2026-10-20 踩到：只认第一种，服务器那 5 份明明带库却显示"无库" ✗
DB_CANDIDATES = ("data/index/index.db", "index.db")


def db_path_of(snapshot_dir: Path) -> Path | None:
    """这份快照里的索引库在哪（没有就返回 None）✓"""
    for rel in DB_CANDIDATES:
        p = Path(snapshot_dir) / rel
        if p.exists():
            return p
    return None


def pending_path(root: Path) -> Path:
    return Path(root) / "backups" / PENDING_NAME


def read_pending(root: Path) -> dict | None:
    p = pending_path(root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return None


def write_pending(root: Path, name: str, code: bool, db: bool, by: str = "") -> dict:
    """登记一个待还原（真正的替换在下次启动前做）✓"""
    root = Path(root)
    snap = root / "backups" / str(name)
    if not snap.is_dir():
        raise ValueError(f"没有这份快照：{name}")
    data = {"name": str(name), "code": bool(code), "db": bool(db),
            "requested": time.time(), "by": str(by)}
    p = pending_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def cancel_pending(root: Path) -> bool:
    p = pending_path(root)
    if p.exists():
        p.unlink()
        return True
    return False


def apply_pending(root: Path, log=print) -> dict | None:          # noqa: ANN001
    """执行待还原（**只在服务启动前调用**）✓ 返回执行结果，没有待办就返回 None。"""
    root = Path(root)
    data = read_pending(root)
    if not data:
        return None
    name = str(data.get("name") or "")
    snap = root / "backups" / name
    out = {"name": name, "db": False, "code": False, "errors": [],
           "backup_of_current": ""}
    if not snap.is_dir():
        out["errors"].append(f"快照不存在：{snap}")
        cancel_pending(root)
        log(f"[还原] 失败：{out['errors'][-1]}")
        return out

    stamp = time.strftime("%Y%m%d-%H%M%S")

    # ---- ① 索引库 ----
    if data.get("db"):
        src = db_path_of(snap)
        live = root / "data" / "index" / "index.db"
        if not src:
            out["errors"].append("这份快照里没有索引库（可能已被清理工具删掉了）")
        else:
            if live.exists():
                safety = live.with_name(f"index-before-restore-{stamp}.db")
                try:
                    shutil.copy2(live, safety)
                    out["backup_of_current"] = safety.name
                    log(f"[还原] 现有索引已另存：{safety.name}")
                except OSError as exc:
                    out["errors"].append(f"备份现有库失败：{exc}")
            try:
                for suffix in ("-wal", "-shm"):
                    stray = live.with_name(live.name + suffix)
                    if stray.exists():
                        stray.unlink()
                shutil.copy2(src, live)
                out["db"] = True
                log(f"[还原] 索引库已换成 {name} 里的那一份 ✓")
            except OSError as exc:
                out["errors"].append(f"覆盖索引库失败：{exc}")

    # ---- ② 代码（只换 app/，不动 config.ini / run / docs）----
    if data.get("code"):
        src_app = snap / "app"
        dst_app = root / "app"
        if not src_app.is_dir():
            out["errors"].append("这份快照里没有 app/ 目录")
        else:
            try:
                shutil.copytree(src_app, dst_app, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                out["code"] = True
                log(f"[还原] app/ 已换成 {name} 里的那一版 ✓")
            except OSError as exc:
                out["errors"].append(f"覆盖代码失败：{exc}")

    cancel_pending(root)
    if out["errors"]:
        log("[还原] 有错误：" + "；".join(out["errors"]))
    return out


def main() -> int:
    """命令行：python -m app.snapshot_restore [--apply]"""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的执行待还原")
    ap.add_argument("--status", action="store_true", help="只看有没有待还原")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    data = read_pending(root)
    if args.status or not args.apply:
        if data:
            print(f"有待还原：{data['name']}（代码={data['code']}，库={data['db']}）")
        else:
            print("没有待还原")
        return 0
    res = apply_pending(root)
    if not res:
        print("没有待还原")
        return 0
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
