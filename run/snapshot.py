"""给项目拍一个「可回退」的快照。

装什么（都是不能轻易再生的）：
  · 源码与配置：app/、docs/、根目录的 .bat/.ini/.md/.txt、安装PDF原版式.bat
  · 测试工具：run/（跳过 node_modules 和临时文件）
  · 人脸标注数据：face-lab 的 .json/.jpg（跳过 382MB 的 .npz 中间产物、跳过可重装的 libs）
  · 人脸权重：face-lab/models（107MB，文档说要从发布包或 ModelScope 取，留着省事）
  · **索引库**：data/index/index.db —— 用 sqlite3 在线备份 API 拿一致性快照
    （直接复制正在被写入的 .db 可能拿到坏文件；项目自带的 backup.py 也是这么做的）

不装什么（说明理由，写进 MANIFEST）：
  venv/models/.cache/.browser（几个 GB，可重建）、data/index 里 19 个历史备份（旧快照）、
  face-lab 的 .npz、logs、dist、__pycache__、node_modules

用法：venv\\Scripts\\python.exe run\\snapshot.py [备注]
产物：backups\\snapshot-<时间戳>\\  +  MANIFEST.txt  +  SNAPSHOT-README.txt
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"venv", "models", ".cache", ".browser", "logs", "dist", "backups",
             "__pycache__", "node_modules", ".git", "out-trial"}
# face-lab 里可以再生的中间产物
SKIP_SUFFIX = {".npz", ".pyc", ".pyo", ".log"}
# 关键文件：快照里记哈希，将来能验证是不是同一份
KEY_FILES = ["app/static/search.html", "app/filesearch.py", "app/store.py",
             "app/model.py", "app/close_ui.py", "app/server.py",
             "stop.bat", "start.bat", "search.bat", "app/run_server.bat",
             "config.ini", "requirements.txt", "安装PDF原版式.bat",
             "app/static/settings.js"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def counts(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    for table in ("files", "chunks", "faces", "people", "folders"):
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[table] = None
    try:
        out["kinds"] = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM chunks GROUP BY kind").fetchall())
    except sqlite3.Error:
        out["kinds"] = {}
    conn.close()
    return out


def copy_tree(src: Path, dst: Path, skip_dirs: set[str], skip_suffix: set[str]) -> tuple[int, int]:
    files = total = 0
    try:
        items = sorted(src.rglob("*"))
    except (PermissionError, OSError) as exc:
        print(f"   [警告] 目录读不全：{exc}")
        items = sorted(src.glob("*"))
    for item in items:
        rel = item.relative_to(src)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if item.is_dir():
            continue
        if item.suffix.lower() in skip_suffix:
            continue
        if item.name.startswith("_"):                 # run/ 里的临时文件
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, target)
        except (PermissionError, OSError) as exc:
            print(f"   [跳过] {rel}（{exc.__class__.__name__}）")
            continue
        files += 1
        total += item.stat().st_size
    return files, total


def main() -> int:
    note = " ".join(sys.argv[1:]).strip() or "手工快照"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = ROOT / "backups" / f"snapshot-{stamp}"
    if dest.exists():
        print(f"[错误] 目标已存在：{dest}")
        return 1
    print(f"快照目录：{dest}\n备注：{note}\n")

    # ---- 1. 索引库：在线一致性备份 ----
    live = ROOT / "data" / "index" / "index.db"
    dest_db = dest / "data" / "index" / "index.db"
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    print("[1/4] 备份索引库（sqlite3 在线备份）…")
    src_conn = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dest_db))
    with dst_conn:
        src_conn.backup(dst_conn)
        dst_conn.execute("PRAGMA journal_mode=DELETE")      # 干净的单文件
    dst_conn.close()
    src_conn.close()
    print(f"   索引库 {live.stat().st_size / 1048576:.1f} MB → "
          f"{dest_db.stat().st_size / 1048576:.1f} MB")

    # ---- 2. 源码 / 文档 / 配置 ----
    print("[2/4] 复制源码、文档、配置、测试工具…")
    total_files = total_bytes = 0
    # face-lab 要单独一套排除规则：那里的 models\ 是人脸权重（107MB，别丢），
    # 而顶层的 models\ 是 25GB 的大模型（要跳过）—— 同名不同命，不能共用一张表。
    tree_skips = {
        "face-lab": {"__pycache__", ".git", "node_modules", "out-trial"},
    }
    for sub in ("app", "docs", "run", "face-lab"):
        base = ROOT / sub
        if not base.exists():
            continue
        n, size = copy_tree(base, dest / sub,
                            tree_skips.get(sub, SKIP_DIRS), SKIP_SUFFIX)
        print(f"   {sub:<10} {n:4d} 个文件  {size / 1048576:7.1f} MB")
        total_files += n
        total_bytes += size
    for item in sorted(ROOT.iterdir()):
        if not item.is_file() or item.name.startswith("_"):
            continue
        if item.suffix.lower() in {".log", ".db", ".zip"}:
            continue
        shutil.copy2(item, dest / item.name)
        total_files += 1
        total_bytes += item.stat().st_size
    print(f"   根目录文件   {total_files:4d} 个（累计）  {total_bytes / 1048576:.1f} MB")

    # ---- 3. MANIFEST ----
    print("[3/4] 写 MANIFEST 与还原说明…")
    live_counts = counts(live)
    snap_counts = counts(dest_db)
    hashes = {}
    for rel in KEY_FILES:
        p = dest / rel
        if p.exists():
            hashes[rel] = sha256(p)
    manifest = {
        "时间": stamp,
        "备注": note,
        "项目": str(ROOT),
        "包含": {
            "源码与文档": ["app/", "docs/", "根目录的 .bat/.ini/.md/.txt"],
            "测试工具": "run/（已跳过 node_modules 与临时文件）",
            "人脸数据": "face-lab 的 .json/.jpg + models/（已跳过 .npz 中间产物与 libs/）",
            "索引库": "data/index/index.db（sqlite3 在线备份，单文件无 WAL 尾巴）",
        },
        "未包含及原因": {
            "venv/ models/ .cache/ .browser/": "几个 GB，setup.bat 可重建",
            "data/index/index-backup-*.db 等 19 份历史备份": "它们是更早的快照，不属于本次状态",
            "face-lab/**/*.npz": "离线人脸工具的中间产物，可从 index.db 重建",
            "face-lab/libs/": "pip --target 装的依赖，可重装；且当前权限读不到",
            "logs/ dist/ __pycache__/ run/domtest/node_modules/": "日志、旧发布包、缓存、可重装依赖",
        },
        "索引库统计": {"快照": snap_counts, "当时线上": live_counts,
                       "一致": snap_counts == live_counts},
        "关键文件 SHA256": hashes,
    }
    (dest / "MANIFEST.txt").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    readme = f"""这个目录是本项目在 {stamp} 的完整代码 + 索引库快照
（备注：{note}）

怎么用
------
1) 只想看/对比代码：直接用这个目录里的 app\\、docs\\、*.bat 就行。

2) 想「整项目回退」到这一刻：
   · 先停服务：双击 stop.bat
   · 把当前项目的 app\\、docs\\、根目录的 .bat/.ini/.md 换成快照里的同名内容
   · 索引库要单独换：
       拷 backups\\snapshot-{stamp}\\data\\index\\index.db
       覆盖到 项目\\data\\index\\index.db
       （覆盖前先把现在的 index.db 改名留一份，别再丢一次）
   · 再双击 search.bat 启动

3) 只想回退索引库：同上第 2 条后半段。

注意
----
· 快照里的 index.db 是**一致性快照**（用 sqlite3 在线备份 API 取的），
  没有 -wal/-shm 尾巴，可以直接当主库用。
· 里面不含 venv/models/.cache，也不含 face-lab 的 .npz 中间产物和 libs\\ 依赖：
  这些要么能重建，要么能重装，不需要进快照。
· 关键文件哈希记在 MANIFEST.txt 里，将来可以核对是不是同一份。
· 这个目录在 backups\\ 下，发布打包（docs\\make_package.py）会自动跳过它。
"""
    (dest / "SNAPSHOT-README.txt").write_text(readme, encoding="utf-8")

    # ---- 4. 校验 ----
    print("[4/4] 校验…")
    n_files = sum(1 for f in dest.rglob("*") if f.is_file())
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    same = snap_counts == live_counts
    print(f"   文件数 {n_files}，总大小 {size / 1048576:.1f} MB")
    print(f"   索引库统计一致：{same}")
    if not same:
        print(f"      快照 {snap_counts}")
        print(f"      线上 {live_counts}")
    bad = [rel for rel, want in hashes.items() if sha256(dest / rel) != want]
    print(f"   关键文件哈希复核：{'全部一致' if not bad else '不一致 ' + str(bad)}")
    print(f"\n完成：{dest}")
    print("   （MANIFEST.txt 里写了包含/未包含清单和哈希）")
    return 0 if (same and not bad) else 1


if __name__ == "__main__":
    raise SystemExit(main())
