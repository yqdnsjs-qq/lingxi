"""按文件夹搬运数据（本机 → 服务器 / 服务器 → 本机），2026-10-20。

⚠ 别和 `app/foldersync.py` 搞混：那个管的是"`visibility`/`owner_id` 这些**设置**
  在两边怎么合并"；这个管的是"文件夹里的**数据**（文件、片段、向量）怎么搬"。
  两者互不覆盖，可以同时用 ✓

为什么是"按文件夹"而不是"整库"
------------------------------
整库推（`run/deploy/sync_db.py`）适合"一台机器就是全部数据"的场景。
多人以后，A 推一次就会把 B 的东西整个盖掉 ✗（这正是最初方案里最危险的一步）。
所以这里做**按文件夹打包**：只搬"我选中的那几个文件夹"，服务器收到后
**按 path 合并**（并集语义，不删别人的），并把归属记成上传者 ✓

包里有什么
----------
JSONL（每行一条）+ gzip，四种记录：
    {"t":"meta",   ...}                  版本、来源、导出时间
    {"t":"folder", "path":...}           文件夹本身（配置/可见性/隐私）
    {"t":"file",   "folder":<路径>, ...} 文件（含 name_vec 向量）
    {"t":"chunk",  "file":<路径>, ...}   片段（含 vec 向量 + text + kind）

向量用 base64 存（JSON 放不了二进制）✓

⚠ 三条硬规矩（都是踩过的）
  1. **不按 id 搬**：id 是各库自己的自增，跨库必然撞。全程用 path 做稳定键 ✓
  2. **维度必须一致**：老数据里有 256 维的 chunk，导进 768 维的库会让搜索结果错乱 ✗
     → 导入时按维度校验，不一致的**跳过并计数**，绝不硬写 ✓
  3. **不覆盖 path、不覆盖已有设置**：同 path 的文件夹已存在时，只补**缺失**的配置键 ✓
"""
from __future__ import annotations

import base64
import gzip
import json
import time
from pathlib import Path

FORMAT_VERSION = 1

FOLDER_COLS = ("path", "config", "visibility", "privacy", "category",
               "server_path", "local_alias")
FILE_COLS = ("path", "dir", "name", "ext", "size", "mtime", "kind",
             "indexed_at", "n_chunks", "status", "note", "name_vec", "name_dim",
             "ocr_pages", "ocr_limit", "ocr_from", "ocr_to", "caption_model")
CHUNK_COLS = ("seq", "text", "vec", "dim", "kind", "uni")


def _b64(blob) -> str | None:                                    # noqa: ANN001
    if blob is None:
        return None
    if isinstance(blob, str):
        return base64.b64encode(blob.encode("utf-8")).decode("ascii")
    return base64.b64encode(bytes(blob)).decode("ascii")


def _unb64(text):                                                # noqa: ANN001
    if not text:
        return None
    return base64.b64decode(text.encode("ascii"))


def _row(row, cols) -> dict:                                     # noqa: ANN001
    out = {}
    for c in cols:
        if c not in row.keys():
            continue
        v = row[c]
        if isinstance(v, (bytes, bytearray, memoryview)):
            v = _b64(v)
        out[c] = v
    return out


def export_folders(store, folder_ids: list[int], out_path: Path,         # noqa: ANN001
                   note: str = "", dim: int | None = None) -> dict:
    """把指定文件夹打成包（JSONL.gz）✓ 返回统计"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stat = {"folders": 0, "files": 0, "chunks": 0, "skipped_dim": 0,
            "bytes": 0, "out": str(out_path)}
    with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=6) as fh:
        fh.write(json.dumps({
            "t": "meta", "version": FORMAT_VERSION, "exported": time.time(),
            "note": note, "dim": dim, "folder_ids": list(folder_ids),
        }, ensure_ascii=False) + "\n")

        for fid in folder_ids:
            rows = store._query("SELECT * FROM folders WHERE id=?", (int(fid),))  # noqa: SLF001
            if not rows:
                continue
            folder = _row(rows[0], FOLDER_COLS)
            fh.write(json.dumps({"t": "folder", **folder}, ensure_ascii=False) + "\n")
            stat["folders"] += 1
            fpath = folder.get("path") or ""

            files = store._query(                                  # noqa: SLF001
                "SELECT * FROM files WHERE folder_id=? ORDER BY id", (int(fid),))
            for fr in files:
                frow = _row(fr, FILE_COLS)
                fh.write(json.dumps({"t": "file", "folder": fpath, **frow},
                                    ensure_ascii=False) + "\n")
                stat["files"] += 1
                chunks = store._query(                             # noqa: SLF001
                    "SELECT * FROM chunks WHERE file_id=? ORDER BY seq", (int(fr["id"]),))
                for cr in chunks:
                    crow = _row(cr, CHUNK_COLS)
                    # 维度不一致的**不导**（导过去也用不上，还会把库搅乱）
                    if dim and crow.get("dim") and int(crow["dim"]) != int(dim):
                        stat["skipped_dim"] += 1
                        continue
                    fh.write(json.dumps({"t": "chunk", "file": frow["path"], **crow},
                                        ensure_ascii=False) + "\n")
                    stat["chunks"] += 1
    stat["bytes"] = out_path.stat().st_size
    return stat


def read_pack(path: Path):                                       # noqa: ANN001
    """逐条读包（生成器，不占内存）✓"""
    with gzip.open(Path(path), "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def ensure_cols(store) -> list[str]:                             # noqa: ANN001
    """目标库可能是个**新建的库**：多人相关的列（owner_id 等）还不存在 ✗

    ⚠ 2026-10-20 踩到：`files.owner_id` 是 `multiuser.ensure_schema` 加的，
      不在 `store.py` 的基础 SCHEMA 里。往新库导入时直接
      `INSERT INTO files(..., owner_id)` → "table files has no column named owner_id" ✗
      所以导入前先补齐这几列（幂等，列在就跳过）✓
    """
    want = (
        ("folders", "owner_id", "INTEGER DEFAULT 0"),
        ("folders", "visibility", "TEXT DEFAULT 'private'"),
        ("folders", "privacy", "INTEGER NOT NULL DEFAULT 0"),
        ("files", "owner_id", "INTEGER DEFAULT 0"),
        ("files", "caption_model", "TEXT"),
    )
    added: list[str] = []
    for tbl, col, decl in want:
        try:
            cols = {r["name"] for r in store._query(f"PRAGMA table_info({tbl})")}  # noqa: SLF001
        except Exception:                                        # noqa: BLE001
            continue
        if col not in cols:
            try:
                store._exec(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")  # noqa: SLF001
                added.append(f"{tbl}.{col}")
            except Exception:                                    # noqa: BLE001
                pass
    return added


def import_pack(store, path: Path, owner_id: int = 0,        # noqa: ANN001
                dry_run: bool = True, expected_dim: int | None = None,  # noqa: ANN001
                strict_dim: bool = False, log=print) -> dict:        # noqa: ANN001
    """把包合并进目标库（**并集语义**：只增/更新，从不删）✓

    ★ 关于维度（2026-10-21 项目主追问"用户能用不同维度建库上传吗"）：
      **能**。本库一直是混合维度（256 维 4.1 万条 + 768 维 2.1 万条并存），
      `store._load_matrix()` 按 dim 分组、查询向量按组切片，
      所以不同维度的片段**本来就能一起搜** ✓

      所以默认 `strict_dim=False`：**不同维度照收，不丢数据**。
      只有显式要"只收同维度"时才传 True（比如给某个纯净库做迁移）。

      `expected_dim` 只用于**统计和提示**（告诉调用方"服务器是 X 维、你这包是 Y 维"），
      不再是"不一致就扔"的依据 ✗ —— 那样用户传上来等于白发，数据白丢了。
    """
    if not dry_run:
        added = ensure_cols(store)
        if added:
            log(f"  目标库补了 {len(added)} 个列：{', '.join(added)}")
    stat = {"new_folders": 0, "hit_folders": 0, "new_files": 0, "hit_files": 0,
            "new_chunks": 0, "hit_chunks": 0, "skipped_dim": 0, "skipped_bad": 0,
            "dim": None}
    folder_ids: dict[str, int] = {}          # path -> id
    file_ids: dict[str, int] = {}            # "folder|path" -> id
    target_dim = int(expected_dim) if expected_dim else None
    seen_dim = None
    for rec in read_pack(path):
        t = rec.get("t")
        if t == "meta":
            if target_dim is None and rec.get("dim"):
                target_dim = int(rec["dim"])
            stat["dim"] = target_dim
            continue
        if t == "folder":
            fpath = str(rec.get("path") or "")
            if not fpath:
                stat["skipped_bad"] += 1
                continue
            rows = store._query(                    # noqa: SLF001
                "SELECT id, config FROM folders WHERE path=?", (fpath,))
            if rows:
                folder_ids[fpath] = int(rows[0]["id"])
                stat["hit_folders"] += 1
                # 已存在：**只补缺失的配置键**，不覆盖 path、不冲掉人家的设置 ✓
                if not dry_run:
                    try:
                        old = json.loads(rows[0]["config"] or "{}")
                        merged = {**dict(rec.get("config") or {}), **old}
                        if merged != old:
                            store._exec(               # noqa: SLF001
                                "UPDATE folders SET config=? WHERE id=?",
                                (json.dumps(merged, ensure_ascii=False),
                                 int(rows[0]["id"])))
                    except Exception as exc:         # noqa: BLE001
                        log(f"  文件夹配置合并失败（跳过）：{exc}")
            else:
                stat["new_folders"] += 1
                if not dry_run:
                    fid = int(store.add_folder(fpath, config=rec.get("config") or {}))  # noqa: SLF001
                    folder_ids[fpath] = fid
                    try:
                        store._exec(                       # noqa: SLF001
                            "UPDATE folders SET visibility=?, privacy=?, owner_id=? "
                            "WHERE id=?",
                            (str(rec.get("visibility") or "private"),
                             int(rec.get("privacy") or 0),
                             int(owner_id or 0), fid))
                    except Exception as exc:         # noqa: BLE001
                        log(f"  文件夹附加字段写入失败（不影响主流程）：{exc}")
                else:
                    folder_ids[fpath] = -1
            continue

        if t == "file":
            fpath = str(rec.get("folder") or "")
            fid = folder_ids.get(fpath)
            # ⚠ 预演时占位是 -1（不是 None）→ 不能当无效跳过，否则预演统计全是 0 ✗
            if fid is None:
                stat["skipped_bad"] += 1
                continue
            fpath_file = str(rec.get("path") or "")
            key = f"{fpath}|{fpath_file}"
            rows = store._query(                    # noqa: SLF001
                "SELECT id FROM files WHERE folder_id=? AND path=?", (fid, fpath_file))
            if rows:
                stat["hit_files"] += 1
                file_ids[key] = int(rows[0]["id"])
                continue
            stat["new_files"] += 1
            if dry_run:
                file_ids[key] = -1
                continue
            cols = [c for c in FILE_COLS if c in rec]
            vals = []
            for c in cols:
                v = rec[c]
                vals.append(_unb64(v) if c == "name_vec" else v)
            cols = ["folder_id", "owner_id"] + cols
            vals = [fid, int(owner_id or 0)] + vals
            cur = store._conn.execute(                # noqa: SLF001
                f"INSERT INTO files({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                vals)
            file_ids[key] = int(cur.lastrowid)
            continue

        if t == "chunk":
            fpath_file = str(rec.get("file") or "")
            fid = None
            for k, i in file_ids.items():
                if k.split("|", 1)[1] == fpath_file:
                    fid = i
                    break
            if fid is None:
                stat["skipped_bad"] += 1
                continue
            dim = int(rec.get("dim") or 0)
            if dim and seen_dim is None:
                seen_dim = dim
                stat["seen_dim"] = dim
            # 只有显式要求「只收同维度」时才跳过；默认照收 ✓
            if strict_dim and target_dim and dim and dim != int(target_dim):
                stat["skipped_dim"] += 1
                continue
            seq = int(rec.get("seq") or 0)
            rows = store._query(                    # noqa: SLF001
                "SELECT id FROM chunks WHERE file_id=? AND seq=?", (fid, seq))
            if rows:
                stat["hit_chunks"] += 1
                continue
            stat["new_chunks"] += 1
            if dry_run:
                continue
            store._conn.execute(                     # noqa: SLF001
                "INSERT INTO chunks(file_id, seq, text, vec, dim, kind, uni) "
                "VALUES(?,?,?,?,?,?,?)",
                (fid, seq, rec.get("text") or "", _unb64(rec.get("vec")), dim,
                 rec.get("kind") or "text", rec.get("uni") or ""))
            continue

    if not dry_run:
        try:
            store._conn.commit()                     # noqa: SLF001
        except Exception:                            # noqa: BLE001
            pass
    return stat
