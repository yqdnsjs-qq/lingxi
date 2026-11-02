"""索引库的**增量**备份（页级差异 + 份数轮换 + 固定份保护）。

为什么不能直接用 app/backup.py 那套（2026-10-20 项目主提的新要求）
--------------------------------------------------------------
项目主要的是：**定时备份 + 本地与服务器各一份（双重保证）+ 增量不浪费硬盘
+ 保留份数可自定义（比如 10 份，循环替换旧的）+ 能固定某几份不被替换** ✗

老做法每份都是**完整拷贝**：库现在 442 MB，留 10 份就是 4.4 GB，
一天备份几次很快就吃掉几十 GB ✗（实测 run\\deploy 下已经躺着 800 MB 的
_local_db_backup-*.db，data\\index 下还有十几份 262 MB 的旧备份）

这里的做法
----------
SQLite 库是**定长页**组成的（本项目 page_size = 4096）。备份时：
  1. 用 SQLite 官方在线备份接口做一份**一致性快照**（服务开着也安全）
  2. 把快照按页切开，逐页算 8 字节摘要
  3. 和**上一份备份**的摘要表比 → 只有变化的页要存
  4. 一份全量（基线）+ 若干份"只含变化页"的增量

实测变化量：索引库日常增长主要是新片段，一次几 MB 到几十 MB，
不是 442 MB —— 所以增量能省掉 90% 以上 ✓

恢复：从目标份回溯到最近的"全量"，把增量里的页**按页号覆盖**上去，
      最后按原文件长度截断 → 与当时那份快照逐字节一致（`verify` 会校验）✓

轮换（keep 份）
---------------
只保留最新 N 份**未被固定**的；被 `pin` 过的永不删除 ✓
删除时若链上第一份变成了增量，会先把它**重建为新的全量基线**再删旧份，
所以任何时候恢复都不需要"已经不存在的祖先" ✓

目录长这样（默认 backups\\db\\）：
    db\\
      20261020-030000\\
          meta.json     这一份的说明（类型/父份/页数/长度/摘要/固定标记…）
          full.db       全量时才有的完整快照
          patch.z       增量时才有的变化页
          pages.bin     这一份的逐页摘要表（下一份拿它做比较）
      20261020-090000\\ …
      last.json         上次检查/上次真正存盘的时间（没变化时只更新它）

⚠ 与 app/backup.py 的关系：那个是老的全量备份（给 backup.bat 用，能恢复），
   这里**不动它**，两套可以并存；新功能都在本模块里 ✓
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import time
import zlib
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.config import Config
else:
    from .config import Config

# 增量文件格式：EG2P + 版本 1 + 页大小 + 变化页数，后面是 (页号,该页长度) 表 + zlib 数据
MAGIC = b"EG2P"
FMT_VERSION = 1
HASH_SIZE = 8                      # 每页摘要 8 字节（blake2b）→ 10 万页才 800 KB
DEFAULT_KEEP = 10
DEFAULT_SUBDIR = "db"


# ============================================================== 小工具
def _log(msg: str) -> None:
    print(msg, flush=True)


def _hash_page(buf: bytes) -> bytes:
    return hashlib.blake2b(buf, digest_size=HASH_SIZE).digest()


def page_size_of(path: Path) -> int:
    """从 SQLite 文件头读页大小（offset 16，2 字节大端；值 1 表示 65536）。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(100)
    except OSError:
        return 4096
    if len(head) < 20:
        return 4096
    raw = int.from_bytes(head[16:18], "big")
    if raw == 1:
        return 65536
    if 512 <= raw <= 65536 and raw & (raw - 1) == 0:
        return raw
    return 4096


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _snapshot(src: Path, dest: Path) -> None:
    """用 SQLite 在线备份接口做一致性快照（服务运行中也安全）✓

    备份落成 DELETE 日志模式：备份就是干净的单文件，不带 -wal 尾巴 ✓
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
        target.commit()
    finally:
        target.close()
        source.close()
    for suffix in ("-wal", "-shm"):
        stray = dest.with_name(dest.name + suffix)
        if stray.exists():
            stray.unlink()


def _page_hashes(path: Path, page_size: int) -> tuple[bytes, int]:
    """逐页算摘要，返回 (摘要表字节, 页数)✓ 内存只用一份摘要表（10 万页 800 KB）"""
    out = bytearray()
    count = 0
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(page_size)
            if not buf:
                break
            out += _hash_page(buf)
            count += 1
    return bytes(out), count


# ============================================================== 目录与索引
def backup_root(cfg) -> Path:
    """备份根目录：config.ini 的 [backup] dir（相对项目根），默认 backups\\db"""
    raw = str(_setting(cfg, "dir", "") or "").strip() or DEFAULT_SUBDIR
    p = Path(raw)
    return p if p.is_absolute() else (cfg.root / "backups" / p)


def _meta_of(folder: Path) -> dict | None:
    meta = folder / "meta.json"
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["folder"] = folder
        return data
    except Exception:                                            # noqa: BLE001
        return None


def list_sets(cfg) -> list[dict]:
    """列出所有备份份（按时间从旧到新）✓"""
    root = backup_root(cfg)
    if not root.exists():
        return []
    items = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        meta = _meta_of(folder)
        if meta:
            items.append(meta)
    items.sort(key=lambda m: str(m.get("stamp") or m["folder"].name))
    return items


def _latest(cfg) -> dict | None:
    items = list_sets(cfg)
    return items[-1] if items else None


def _read_last(cfg) -> dict:
    path = backup_root(cfg) / "last.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return {}


def _write_last(cfg, data: dict) -> None:
    root = backup_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "last.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ============================================================== 存一份
def save(cfg, note: str = "", force: bool = False) -> dict:
    """做一份备份。没有变化时**不落盘**（省空间），只更新 last.json ✓

    返回 {ok, changed, stamp, kind, changed_pages, stored_bytes, elapsed}
    """
    started = time.time()
    src = cfg.db_path
    if not src.exists():
        raise RuntimeError(f"找不到索引库：{src}（还没建过索引，不需要备份）")

    root = backup_root(cfg)
    tmp_dir = root / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    snap = tmp_dir / "cur.db"

    try:
        _snapshot(src, snap)
        page_size = page_size_of(snap)
        hashes, page_count = _page_hashes(snap, page_size)
        size = snap.stat().st_size
        parent = _latest(cfg)
        parent_folder = parent["folder"] if parent else None

        # 与上一份比：摘要表完全一样 → 库没变，不必再存一份 ✓
        if (parent and not force
                and int(parent.get("page_size") or 0) == page_size
                and int(parent.get("page_count") or 0) == page_count
                and (parent_folder / "pages.bin").exists()
                and (parent_folder / "pages.bin").read_bytes() == hashes):
            last = _read_last(cfg)
            last.update({"last_check": time.time(), "last_result": "没变化（跳过）"})
            _write_last(cfg, last)
            return {"ok": True, "changed": False, "stamp": "",
                    "kind": "", "changed_pages": 0, "stored_bytes": 0,
                    "elapsed": round(time.time() - started, 1),
                    "message": "库内容与上一份一致，没有新存备份"}

        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = root / stamp
        # 同一秒内重复触发时加后缀，绝不覆盖已有份 ✓
        seq = 1
        while folder.exists():
            seq += 1
            folder = root / f"{stamp}-{seq}"
        folder.mkdir(parents=True, exist_ok=True)

        sha = file_sha256(snap)
        changed_pages = 0
        stored = 0

        if parent is None:
            kind = "full"
            shutil.move(str(snap), str(folder / "full.db"))
            stored = (folder / "full.db").stat().st_size
        else:
            kind = "incr"
            p_pages = parent_folder / "pages.bin"
            old_hashes = p_pages.read_bytes() if p_pages.exists() else b""
            # ⚠ 2026-10-20 实测踩到：原来这里要求"页数必须一样"才逐页比较 ✗
            #   结果库只要**多出一页**（打标写了几十条就多几十页），
            #   就退化成"全部页都算变化" → 108146 页全存、落盘 202 MB ✗
            #   增量备份的意义直接没了。改成**按页号逐个比**：
            #   旧表长度不够的位置当作"新页"，自然就算变化 ✓
            entries: list[tuple[int, int]] = []
            payload = io.BytesIO()
            with open(snap, "rb") as fh:
                page_no = 0
                while True:
                    buf = fh.read(page_size)
                    if not buf:
                        break
                    old = old_hashes[page_no * HASH_SIZE:(page_no + 1) * HASH_SIZE]
                    if not old or old != _hash_page(buf):
                        entries.append((page_no, len(buf)))
                        payload.write(buf)
                    page_no += 1
            if not entries:
                # 摘要表不同但单页都没变（只可能是页数变了）→ 退化成全量，保证能恢复 ✓
                kind = "full"
                shutil.move(str(snap), str(folder / "full.db"))
                stored = (folder / "full.db").stat().st_size
            else:
                changed_pages = len(entries)
                head = (MAGIC + bytes([FMT_VERSION])
                        + struct.pack(">II", page_size, len(entries))
                        + b"".join(struct.pack(">II", a, b) for a, b in entries))
                data_z = zlib.compress(payload.getvalue(), 6)
                (folder / "patch.z").write_bytes(head + data_z)
                (folder / "pages.bin").write_bytes(hashes)
                stored = (folder / "patch.z").stat().st_size
                snap.unlink()

        if kind == "full":
            (folder / "pages.bin").write_bytes(hashes)

        meta = {
            "stamp": folder.name,
            "kind": kind,
            "parent": (parent["stamp"] if (parent and kind == "incr") else ""),
            "page_size": page_size,
            "page_count": page_count,
            "file_size": size,
            "sha256": sha,
            "created": time.time(),
            "note": str(note)[:200],
            "pinned": False,
            "changed_pages": changed_pages,
            "stored_bytes": stored,
            "source": str(src),
        }
        (folder / "meta.json").write_text(
            json.dumps({k: v for k, v in meta.items() if k != "folder"},
                       ensure_ascii=False, indent=2), encoding="utf-8")

        _write_last(cfg, {"last_check": time.time(), "last_saved": time.time(),
                          "last_result": f"{kind} {folder.name}",
                          "last_stamp": folder.name})

        # 顺手轮换（也可以在单独的定时任务里跑）
        try:
            prune(cfg)
        except Exception as exc:                                 # noqa: BLE001
            _log(f"  [提醒] 轮换旧备份时出错（不影响这份）：{exc}")

        return {"ok": True, "changed": True, "stamp": folder.name, "kind": kind,
                "changed_pages": changed_pages, "stored_bytes": stored,
                "full_bytes": size,
                "elapsed": round(time.time() - started, 1),
                "message": f"已存 {kind} 备份 {folder.name}"}
    finally:
        for leftover in (tmp_dir / "cur.db", tmp_dir / "cur.db-wal",
                         tmp_dir / "cur.db-shm"):
            try:
                if leftover.exists():
                    leftover.unlink()
            except OSError:
                pass


# ============================================================== 恢复 / 校验
def _chain(cfg, stamp: str) -> list[dict]:
    """从目标份回溯到全量，返回**从旧到新**的链 ✓"""
    items = {m["stamp"]: m for m in list_sets(cfg)}
    if stamp not in items:
        raise RuntimeError(f"没有这份备份：{stamp}")
    chain: list[dict] = []
    cur = items[stamp]
    seen: set[str] = set()
    while True:
        chain.append(cur)
        if cur.get("kind") == "full":
            break
        parent = str(cur.get("parent") or "")
        if not parent or parent in seen:
            raise RuntimeError(f"备份链断了（{cur['stamp']} 的父份 {parent or '空'} 不存在）")
        seen.add(parent)
        if parent not in items:
            raise RuntimeError(f"备份链断了：找不到父份 {parent}")
        cur = items[parent]
    chain.reverse()
    return chain


def restore(cfg, stamp: str, dest: Path) -> Path:
    """把某份备份**重建**成完整数据库文件（默认不覆盖线上库）✓

    ⚠ 如果 dest 就是当前正用的库（`--to` 指到 data\\index\\index.db），
      会先把现有库另存一份 `index-before-restore-<时间>.db` 再动手 ——
      重建要几百 MB、万一中途断电，手上不能只剩一个半成品 ✗
    ⚠ 覆盖线上库**必须先停服务**：Windows 上被占用的文件写不进去，
      而且服务可能把半成品当成新库打开。恢复流程见 docs\\交接说明-定时备份-20261020.md
    """
    chain = _chain(cfg, stamp)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            same_as_live = dest.resolve() == Path(cfg.db_path).resolve()
        except OSError:
            same_as_live = False
        if same_as_live:
            stamp_now = time.strftime("%Y%m%d-%H%M%S")
            safety = dest.parent / f"index-before-restore-{stamp_now}.db"
            shutil.copy2(dest, safety)
            _log(f"  现有索引已另存为：{safety.name}")
        dest.unlink()
    base = chain[0]
    base_file = base["folder"] / "full.db"
    if not base_file.exists():
        raise RuntimeError(f"基线缺失：{base_file}")
    shutil.copy2(base_file, dest)

    with open(dest, "r+b") as out:
        for item in chain[1:]:
            patch = item["folder"] / "patch.z"
            if not patch.exists():
                raise RuntimeError(f"增量文件缺失：{patch}")
            raw = patch.read_bytes()
            if raw[:4] != MAGIC or raw[4] != FMT_VERSION:
                raise RuntimeError(f"增量文件格式不对：{patch}")
            page_size, n = struct.unpack(">II", raw[5:13])
            table = raw[13:13 + n * 8]
            blob = zlib.decompress(raw[13 + n * 8:])
            off = 0
            for i in range(n):
                page_no, length = struct.unpack(">II", table[i * 8:(i + 1) * 8])
                out.seek(page_no * page_size)
                out.write(blob[off:off + length])
                off += length
    # 按当时的文件长度截断（最后一页可能不是整页）
    with open(dest, "r+b") as out:
        out.truncate(int(chain[-1]["file_size"]))
    return dest


def verify(cfg, stamp: str) -> dict:
    """重建一份到临时文件并与 meta 里记的 sha256 比对 ✓"""
    tmp = backup_root(cfg) / ".tmp" / f"verify-{stamp}.db"
    try:
        restore(cfg, stamp, tmp)
        got = file_sha256(tmp)
        want = str(_meta_by_stamp(cfg, stamp).get("sha256") or "")
        return {"ok": got == want, "stamp": stamp, "sha256": got, "expected": want}
    finally:
        for leftover in (tmp, tmp.with_name(tmp.name + "-wal"),
                         tmp.with_name(tmp.name + "-shm")):
            try:
                if leftover.exists():
                    leftover.unlink()
            except OSError:
                pass


def _meta_by_stamp(cfg, stamp: str) -> dict:
    for m in list_sets(cfg):
        if m["stamp"] == stamp:
            return m
    raise RuntimeError(f"没有这份备份：{stamp}")


# ============================================================== 固定 / 轮换
def pin(cfg, stamp: str, pinned: bool = True) -> dict:
    """把某份标成"固定"（轮换时永不删除）✓"""
    meta = _meta_by_stamp(cfg, stamp)
    folder = meta["folder"]
    data = {k: v for k, v in meta.items() if k != "folder"}
    data["pinned"] = bool(pinned)
    (folder / "meta.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _promote(cfg, stamp: str) -> dict:
    """把一份增量**重建为全量**（轮换要删掉它前面的份时必须先做这一步）✓

    做法：用它自己（连同祖先）重建出完整库 → 存成 full.db → 改 meta → 删 patch。
    此时它的祖先还在（调用方保证先 promote 再删）✓
    """
    meta = _meta_by_stamp(cfg, stamp)
    if meta.get("kind") == "full":
        return meta
    folder = meta["folder"]
    tmp = backup_root(cfg) / ".tmp" / f"promote-{stamp}.db"
    try:
        restore(cfg, stamp, tmp)
        target = folder / "full.db"
        if target.exists():
            target.unlink()
        shutil.move(str(tmp), str(target))
    finally:
        for leftover in (tmp, tmp.with_name(tmp.name + "-wal"),
                         tmp.with_name(tmp.name + "-shm")):
            try:
                if leftover.exists():
                    leftover.unlink()
            except OSError:
                pass
    data = {k: v for k, v in meta.items() if k != "folder"}
    data["kind"] = "full"
    data["parent"] = ""
    data["stored_bytes"] = (folder / "full.db").stat().st_size
    data["changed_pages"] = 0
    (folder / "meta.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    patch = folder / "patch.z"
    if patch.exists():
        patch.unlink()
    _log(f"  已把 {stamp} 重建为新的全量基线（原来依赖的旧份可以删了）")
    return data


def prune(cfg, keep: int | None = None) -> dict:
    """只保留最新 keep 份（**固定的不算在轮换里**）✓

    数据安全顺序：**先**把剩下的最早一份提升为全量，**再**删旧份 ——
    这样任何时刻都不会出现"恢复时找不到祖先"的情况 ✓
    """
    if keep is None:
        keep = int(_setting(cfg, "keep", DEFAULT_KEEP) or DEFAULT_KEEP)
    keep = max(1, int(keep))
    items = list_sets(cfg)
    pinned = [m for m in items if m.get("pinned")]
    plain = [m for m in items if not m.get("pinned")]
    removed: list[str] = []
    if len(plain) <= keep:
        return {"ok": True, "removed": removed, "kept": len(items),
                "pinned": len(pinned)}

    doomed = plain[:len(plain) - keep]
    doomed_stamps = {m["stamp"] for m in doomed}
    remaining = [m for m in items if m["stamp"] not in doomed_stamps]

    # ① 先保证"留下来的每一份"都能自己站住：
    #    谁的父份要被删掉，就先把谁**重建为全量**（这一步必须在删之前做，
    #    因为重建要用到还没被删的祖先）✓
    #    ⚠ 2026-10-20 自测抓到的 bug：只把"最早那份"提升为全量是不够的 ✗
    #      例：A(固定·全量) → B → C → D，要删 B 时 C 的父份就没了 → 恢复报"链断了" ✗
    keep_stamps = {m["stamp"] for m in remaining}
    for item in list(remaining):
        fresh = _meta_by_stamp(cfg, item["stamp"])
        if fresh.get("kind") == "full":
            continue
        if str(fresh.get("parent") or "") in keep_stamps:
            continue                      # 父份留得住，它自己不用动 ✓
        try:
            _promote(cfg, fresh["stamp"])
        except Exception as exc:                                 # noqa: BLE001
            _log(f"  [警告] 提升全量失败，这次先不删旧份（保护数据）：{exc}")
            return {"ok": False, "removed": [], "kept": len(items),
                    "pinned": len(pinned), "error": str(exc)}

    # ② 再删
    for meta in doomed:
        folder = meta["folder"]
        try:
            shutil.rmtree(folder)
            removed.append(meta["stamp"])
        except OSError as exc:
            _log(f"  [提醒] 删不掉 {folder}：{exc}")
    return {"ok": True, "removed": removed,
            "kept": len(list_sets(cfg)), "pinned": len(pinned)}


# ============================================================== 配置与状态
def _setting(cfg, key: str, default=None):                       # noqa: ANN001
    """读 [backup] 段的项（用 Config 自己的读取口径，缺省就返回 default）✓"""
    try:
        return cfg._get("backup", key, default)                  # noqa: SLF001
    except Exception:                                            # noqa: BLE001
        return default


def status(cfg) -> dict:
    """给界面/命令行看的状态：多少份、占多大、上次什么时候、固定的有哪些 ✓"""
    items = list_sets(cfg)
    total = 0
    for m in items:
        folder = m["folder"]
        for name in ("full.db", "patch.z", "pages.bin"):
            p = folder / name
            if p.exists():
                total += p.stat().st_size
    last = _read_last(cfg)
    newest = items[-1] if items else None
    return {
        "ok": True,
        "dir": str(backup_root(cfg)),
        "count": len(items),
        "pinned": [m["stamp"] for m in items if m.get("pinned")],
        "used_mb": round(total / 1e6, 1),
        "would_be_full_mb": round(
            sum(float(m.get("file_size") or 0) for m in items) / 1e6, 1),
        "keep": int(_setting(cfg, "keep", DEFAULT_KEEP) or DEFAULT_KEEP),
        "interval_hours": float(_setting(cfg, "interval_hours", 6) or 6),
        "enabled": str(_setting(cfg, "enabled", "yes")).lower() in
                   ("1", "yes", "true", "on", "y"),
        "last": last,
        "sets": [{
            "stamp": m["stamp"], "kind": m.get("kind"),
            "parent": m.get("parent") or "", "pinned": bool(m.get("pinned")),
            "created": m.get("created"), "file_size": m.get("file_size"),
            "stored_bytes": m.get("stored_bytes"),
            "changed_pages": m.get("changed_pages"),
            "note": m.get("note") or "",
        } for m in items],
        "newest": newest["stamp"] if newest else "",
    }


def should_run(cfg, now: float | None = None) -> tuple[bool, str]:
    """到点了吗（定时任务每小时的入口用它判断，不用自己算间隔）✓"""
    if str(_setting(cfg, "enabled", "yes")).lower() not in (
            "1", "yes", "true", "on", "y"):
        return False, "总开关关着（config.ini [backup] enabled = no）"
    hours = float(_setting(cfg, "interval_hours", 6) or 6)
    last = _read_last(cfg)
    last_ts = float(last.get("last_saved") or 0)
    now = time.time() if now is None else now
    if not last_ts:
        return True, "还没备份过 → 立刻做一份"
    gap = (now - last_ts) / 3600.0
    if gap >= hours:
        return True, f"距上次 {gap:.1f} 小时 ≥ {hours} 小时"
    return False, f"距上次才 {gap:.1f} 小时（阈值 {hours} 小时），这次跳过"


# ============================================================== 命令行
def _fmt_time(ts) -> str:                                        # noqa: ANN001
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:                                            # noqa: BLE001
        return "?"


def main(argv: list[str]) -> int:
    cfg = Config()
    action = (argv[0] if argv else "status").lower()

    if action in ("save", "backup", "run"):
        note = " ".join(argv[1:])
        # --if-due：给定时任务用；不到点就什么都不做
        if "--if-due" in argv:
            note = note.replace("--if-due", "").strip()
            ok, why = should_run(cfg)
            if not ok:
                _log(f"[跳过] {why}")
                return 0
            _log(f"[该做了] {why}")
        _log("=" * 60)
        _log("  灵犀 · 索引库增量备份")
        _log("=" * 60)
        res = save(cfg, note=note)
        if not res.get("changed"):
            _log(f"  {res.get('message')}")
            return 0
        _log(f"  已存     : {res['stamp']}（{res['kind']}）")
        _log(f"  变化页   : {res['changed_pages']} 页 / "
             f"落盘 {res['stored_bytes'] / 1e6:.2f} MB"
             f"（完整库 {res['full_bytes'] / 1e6:.2f} MB）")
        _log(f"  耗时     : {res['elapsed']} 秒")
        st = status(cfg)
        _log(f"  现有     : {st['count']} 份，占 {st['used_mb']} MB"
             f"（若都是全量得 {st['would_be_full_mb']} MB）")
        return 0

    if action == "status":
        st = status(cfg)
        _log("=" * 60)
        _log("  灵犀 · 增量备份状态")
        _log("=" * 60)
        _log(f"  目录     : {st['dir']}")
        _log(f"  份数     : {st['count']}（保留 {st['keep']} 份，"
             f"每 {st['interval_hours']} 小时一次，"
             f"总开关 {'开' if st['enabled'] else '关'}）")
        _log(f"  占用     : {st['used_mb']} MB"
             f"（全存全量的话要 {st['would_be_full_mb']} MB）")
        _log(f"  固定     : {', '.join(st['pinned']) or '（没有）'}")
        last = st.get("last") or {}
        _log(f"  上次检查 : {_fmt_time(last.get('last_check'))}  "
             f"{last.get('last_result') or ''}")
        _log("")
        for item in st["sets"]:
            flag = "📌" if item["pinned"] else "  "
            _log(f"  {flag} {item['stamp']}  {item['kind']:4s} "
                 f"{(_fmt_time(item['created']))}  "
                 f"{float(item['file_size'] or 0) / 1e6:7.1f} MB  "
                 f"存 {float(item['stored_bytes'] or 0) / 1e6:6.2f} MB  "
                 f"{item['note']}")
        return 0

    if action == "list":
        for item in list_sets(cfg):
            _log(f"{item['stamp']}  {item.get('kind')}  "
                 f"{'固定' if item.get('pinned') else '轮换'}")
        return 0

    if action == "pin":
        if len(argv) < 2:
            _log("用法：backup_db.py pin <份名>   （固定，永不被轮换删掉）")
            return 1
        data = pin(cfg, argv[1], True)
        _log(f"已固定 {data['stamp']}")
        return 0

    if action == "unpin":
        if len(argv) < 2:
            _log("用法：backup_db.py unpin <份名>")
            return 1
        data = pin(cfg, argv[1], False)
        _log(f"已取消固定 {data['stamp']}")
        return 0

    if action == "verify":
        if len(argv) < 2:
            _log("用法：backup_db.py verify <份名>")
            return 1
        res = verify(cfg, argv[1])
        _log(("✓ 校验通过" if res["ok"] else "✗ 校验不一致")
             + f"：{res['stamp']}  sha256={res['sha256'][:16]}…")
        if not res["ok"]:
            _log(f"  期望 {res['expected'][:16]}…")
        return 0 if res["ok"] else 1

    if action == "prune":
        keep = int(argv[1]) if len(argv) > 1 else None
        res = prune(cfg, keep)
        _log(f"轮换完成：删了 {len(res['removed'])} 份，"
             f"现在 {res['kept']} 份（固定 {res['pinned']} 份）")
        for stamp in res["removed"]:
            _log(f"   - {stamp}")
        return 0 if res.get("ok") else 1

    if action == "restore":
        if len(argv) < 2:
            _log("用法：backup_db.py restore <份名> [--to 路径]")
            return 1
        dest = Path(argv[3]) if "--to" in argv and len(argv) > 3 else (
            cfg.root / "data" / "index" / f"restored-{argv[1]}.db")
        out = restore(cfg, argv[1], dest)
        _log(f"已重建到：{out}")
        got = file_sha256(out)
        want = str(_meta_by_stamp(cfg, argv[1]).get("sha256") or "")
        _log("✓ 与备份记录的摘要一致" if got == want else "⚠ 摘要不一致，请复核")
        return 0

    _log("用法：")
    _log("  backup_db.py save [--if-due] [说明]   做一份（无变化则跳过）")
    _log("  backup_db.py status                  看状态/列表")
    _log("  backup_db.py pin <份名> / unpin <份名>  固定 / 取消固定")
    _log("  backup_db.py verify <份名>            重建并校验摘要")
    _log("  backup_db.py prune [份数]             立刻轮换")
    _log("  backup_db.py restore <份名> --to 路径  重建到指定文件")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
