"""文件索引的存储与检索（SQLite + numpy 向量）。

索引全部保存在 data\\index\\index.db 里，关掉程序也不会丢；
下次扫描时按「路径 + 大小 + 修改时间」判断哪些文件需要重新索引。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

# ★ 2026-10-27：目录存在性检查加个短缓存。
#   网络盘（NAS / SMB / NFS）上 os.path.isdir 可能一次卡几秒，
#   19 个文件夹串起来就是十几秒 —— 界面每 30 秒轮询一次会把页面拖死 ✗
#   缓存 15 秒足够；「重新检查目录」那个按钮走 force 路径，不吃缓存 ✓
_EXISTS_TTL = 60.0          # 这么久的缓存直接信
_EXISTS_REF = 300.0         # 60~300 秒：先返回旧值，**后台**再刷一次（不卡界面）
_exists_cache: dict[str, tuple[float, bool]] = {}
_exists_busy: set[str] = set()
_exists_lock = threading.Lock()


def _refresh_async(path: str) -> None:
    """后台重新 stat 一次（同一路径同时只跑一个，避免线程风暴）。"""
    with _exists_lock:
        if path in _exists_busy:
            return
        _exists_busy.add(path)

    def _run() -> None:
        try:
            ok = bool(os.path.isdir(path))
        except OSError:
            ok = False
        _exists_cache[path] = (time.time(), ok)
        with _exists_lock:
            _exists_busy.discard(path)

    threading.Thread(target=_run, daemon=True).start()


def path_exists_cached(path: str, force: bool = False) -> bool:
    """os.path.isdir + 缓存（网络盘慢的时候救命）。

    2026-10-27：19 个网络路径串行 stat 要十几秒，界面每 30 秒轮询一次就卡一次 ✗
    所以：60 秒内直接信缓存；超过就**先返回旧值、后台刷新**
    （stale-while-revalidate）；只有「重新检查目录」按钮才 force 同步查 ✓
    """
    now = time.time()
    hit = _exists_cache.get(path)
    if not force:
        if hit:
            age = now - hit[0]
            if age < _EXISTS_TTL:
                return hit[1]
            if age < _EXISTS_REF:
                _refresh_async(path)
                return hit[1]
    try:
        ok = os.path.isdir(path)
    except OSError:
        ok = False
    _exists_cache[path] = (now, bool(ok))
    return bool(ok)

import numpy as np

log = logging.getLogger("eg2.store")


def _loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  path        TEXT NOT NULL UNIQUE,
  added_at    REAL NOT NULL,
  last_scan   REAL,
  status      TEXT DEFAULT 'idle',
  config      TEXT
);
CREATE TABLE IF NOT EXISTS files(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  folder_id   INTEGER NOT NULL,
  path        TEXT NOT NULL UNIQUE,
  dir         TEXT NOT NULL,
  name        TEXT NOT NULL,
  ext         TEXT,
  size        INTEGER NOT NULL,
  mtime       REAL NOT NULL,
  kind        TEXT,
  indexed_at  REAL,
  n_chunks    INTEGER DEFAULT 0,
  status      TEXT DEFAULT 'ok',
  note        TEXT,
  name_vec    BLOB,
  name_dim    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);

CREATE TABLE IF NOT EXISTS chat_conversation(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL DEFAULT 0,
  title       TEXT NOT NULL DEFAULT '新对话',
  model       TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  archived    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_conv_user ON chat_conversation(user_id, updated_at);

CREATE TABLE IF NOT EXISTS chat_message(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id     INTEGER NOT NULL,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL,
  sources     TEXT,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_message(conv_id, id);
CREATE TABLE IF NOT EXISTS chunks(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id  INTEGER NOT NULL,
  seq      INTEGER NOT NULL,
  text     TEXT,
  vec      BLOB NOT NULL,
  dim      INTEGER NOT NULL,
  kind     TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- 人脸：每张脸一行。person_id 为空表示还没归到某个人
CREATE TABLE IF NOT EXISTS faces(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id    INTEGER NOT NULL,
  person_id  INTEGER,
  x1         REAL, y1 REAL, x2 REAL, y2 REAL,
  prob       REAL,
  vec        BLOB NOT NULL,
  dim        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_faces_file ON faces(file_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);

-- 人物（也包括「非人脸物件」类别，用 kind 区分）
CREATE TABLE IF NOT EXISTS people(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL DEFAULT 'person',   -- person | object
  n_faces    INTEGER DEFAULT 0,
  cover_face INTEGER,
  created    REAL,
  note       TEXT
);

-- 目录 mtime 缓存（多人 ⑥ 索引加速）：目录没变过就不用下去列目录 ✓
-- n_dirs = 上次扫描时这个目录里的**子目录个数**：只有 0（叶子目录）才允许跳过 ✓
-- （新建在深层子目录里的文件不会改父目录 mtime，父目录一跳过就会漏掉新文件 ✗）
-- 只对开了 fast_scan 的文件夹写入；重建索引（force）永远全量 ✓
CREATE TABLE IF NOT EXISTS dir_scan(
  folder_id  INTEGER NOT NULL,
  path       TEXT NOT NULL,
  mtime      REAL NOT NULL,
  n_dirs     INTEGER NOT NULL DEFAULT -1,
  PRIMARY KEY (folder_id, path)
);

-- 插图打标队列（2026-10-19 项目主：白天"只登记"，夜里"全力执行"）✓
-- 为什么单独一张表、不直接写 chunks：
--   · 登记的时候**还没打标**（chunks 里必须有向量）✗
--   · 同一张图要能带着状态走：待办 / 做完 / 失败 / 跳过 ✓
-- sha1 是图片内容的指纹 → 同一张图（哪怕在不同页、不同文件）只标一次 ✓
-- page 记第几页 → 检索命中时能告诉用户"第 N 页那张图" ✓
CREATE TABLE IF NOT EXISTS caption_queue(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id    INTEGER NOT NULL,
  folder_id  INTEGER NOT NULL,
  page       INTEGER,
  xref       INTEGER,
  sha1       TEXT NOT NULL,
  width      INTEGER,
  height     INTEGER,
  state      TEXT NOT NULL DEFAULT 'pending',   -- pending|done|failed|skip
  tries      INTEGER NOT NULL DEFAULT 0,
  note       TEXT,
  created    REAL,
  updated    REAL,
  UNIQUE(file_id, sha1)
);
CREATE INDEX IF NOT EXISTS idx_capq_state ON caption_queue(state);
CREATE INDEX IF NOT EXISTS idx_capq_folder ON caption_queue(folder_id);

-- ★ 2026-10-20 多人版第一步：**按用户存的界面偏好**
--   为什么不在 meta 上加一列 user_id：meta 里还有 dim / fts_built 这类
--   "整库一份"的状态，混在一起会分不清"谁的"。单独一张表最清楚 ✓
--   meta 里的那份 ui_pref.* 继续当**全局默认**（后台任务、未登录时读它）
CREATE TABLE IF NOT EXISTS user_prefs(
  user_id INTEGER NOT NULL,
  key     TEXT NOT NULL,
  value   TEXT,
  PRIMARY KEY(user_id, key)
);
"""

# 老版本数据库的增量升级（列已存在时会报错，忽略即可）
MIGRATIONS = (
    "ALTER TABLE folders ADD COLUMN config TEXT",
    "ALTER TABLE files ADD COLUMN name_dim INTEGER",
    "ALTER TABLE dir_scan ADD COLUMN n_dirs INTEGER NOT NULL DEFAULT -1",
    # ★ 2026-10-19：扫描版 OCR 做到第几页 / 当时的上限
    #   项目主问："调高页码数后，原已 OCR 的页面不会重新 OCR 吧"
    #   → 记下来就能**接着做**，不重做前面的页 ✓
    #   ocr_limit 是"上次做的时候上限是多少"，用来判断"上限调大了、该续做了"✓
    "ALTER TABLE files ADD COLUMN ocr_pages INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE files ADD COLUMN ocr_limit INTEGER NOT NULL DEFAULT 0",
    # ★ 2026-10-19（项目主）：**单份 PDF 单独指定 OCR 页码范围**，
    #   优先级**最高**（个别 > 全局）。0 表示"没单独设过，跟随全局"✓
    #   例：全局设"全部做"，但某份 300 页手册只想做 50~80 页 ✓
    "ALTER TABLE files ADD COLUMN ocr_from INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE files ADD COLUMN ocr_to INTEGER NOT NULL DEFAULT 0",
    # ★ 2026-10-20（项目主）：本机路径 ↔ 服务器路径
    #   "我桌面版用了 \\NAS\photo\家庭相册，它应该旁边有个服务器对应的路径
    #    可以自定义，现在对应 /mnt/nas_photo/家庭相册"
    #   → 存下"这个文件夹在服务器上的路径"，服务器上跑索引/预览时用它 ✓
    #   （数据库两边共享，path 是本机视角，server_path 是服务器视角 ✓）
    "ALTER TABLE folders ADD COLUMN server_path TEXT",
    # ★ 2026-10-20（项目主追加）：**本机路径也能自定义** —— 给"本机实际目录"留一个别名。
    #   "本机的文件夹也需可以自定义设置同步目录"
    #   场景：NAS 改名 / 盘符换了 / 这台机器上挂载点不同 → 不用改主键 path（那会牵连
    #   所有 files.path），填一个 local_alias 就够 ✓ 空 = 就用 path ✓
    "ALTER TABLE folders ADD COLUMN local_alias TEXT",
    # ★ 2026-10-20 下午（项目主）：**单份 PDF 单独指定打标模型**
    #   "pdf打标按钮需要指定文件…而且针对我某些文件隐私效果需要用到本地"
    #   值：''  = 跟随文件夹（folderconf 的 caption_model）
    #       'local'  = 这份文件的插图**不出机器**（证件、内部资料那种）
    #       'remote' = 这份文件走外部 API（快、强）
    #   优先级：文件 > 文件夹 > 全局 ✓
    "ALTER TABLE files ADD COLUMN caption_model TEXT",
    # ★ 2026-10-20 下午（项目主）：**隐私文件夹**
    #   "我的隐私文件统统放在里面，这样设置更方便"
    #   + "这样就是用外部模型也搜索不到"
    #   语义（和 `visibility` 不是一回事 ✗）：
    #     visibility=private → **普通用户看不到**（多人可见性）
    #     privacy=1          → 内容**永不出这台机器**：打标强制走本地视觉模型、
    #                          问答时它的片段**不发给外部模型**（管理员自己也一样）✓
    #   本机搜索仍照常能搜到（不然这个文件夹就没法用了）✓
    "ALTER TABLE folders ADD COLUMN privacy INTEGER NOT NULL DEFAULT 0",
)


def _to_blob(vector: Iterable[float] | np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _slice_normalize(vector: np.ndarray, dim: int) -> np.ndarray | None:
    """取前 dim 维并重新归一化——这正是 Matryoshka 截断的定义。"""
    if dim <= 0 or dim > vector.shape[0]:
        return None
    part = np.ascontiguousarray(vector[:dim], dtype=np.float32)
    norm = float(np.linalg.norm(part))
    if norm <= 1e-9:
        return None
    return part / norm


class Store:
    def __init__(self, cfg, db_path=None):
        self.cfg = cfg
        # ★ 2026-10-20 多人版地基：允许指定库文件 ✓
        #   不传就用 cfg.db_path（**和以前完全一样**，所有现有调用都不受影响）
        #   多人化以后，由 app/userdb.py 按登录用户挑库传进来
        self.path = Path(db_path) if db_path else cfg.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            for statement in MIGRATIONS:
                try:
                    self._conn.execute(statement)
                except sqlite3.OperationalError:
                    pass                       # 列已存在
            self._conn.commit()
        # 关键词检索通道（FTS5）：幂等；建不起来也只是退化成原来的向量 + LIKE ✓
        try:
            from . import ftsindex as _fts                        # noqa: PLC0415

            self.fts_state = _fts.ensure(self._conn)
        except Exception as exc:                                  # noqa: BLE001
            log.warning("FTS5 通道初始化失败（不影响启动）：%s", exc)
            self.fts_state = {"ok": False, "error": str(exc)}

    fts_state: dict = {"ok": False, "error": "还没初始化"}

    # ------------------------------------------------------------ 基础
    def ensure_caption_skip(self) -> None:
        """打标跳过表：只放**解码就失败**的图。

        网络/模型/额度错误一律不记 —— 那些换模型或换网络就该重试。
        """
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS caption_skip ("
                " file_id INTEGER PRIMARY KEY, reason TEXT, added TEXT)")
            self._conn.commit()

    def caption_skip_add(self, file_id: int, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO caption_skip(file_id, reason, added) "
                "VALUES(?,?,datetime('now','localtime'))",
                (int(file_id), str(reason)[:200]))
            self._conn.commit()

    def caption_skip_ids(self) -> set:
        try:
            return {int(r["file_id"]) for r in
                    self._query("SELECT file_id FROM caption_skip")}
        except Exception:
            return set()

    def caption_skip_count(self) -> int:
        try:
            return int(self._query(
                "SELECT COUNT(*) AS n FROM caption_skip")[0]["n"])
        except Exception:
            return 0

    # ================================================ 扫描版 OCR 进度（2026-10-19）
    # 项目主："调高页码数后，原已 OCR 的页面不会重新 OCR 吧"
    # → 记下"已做到第几页"和"当时的上限"，下次从下一页接着做 ✓
    def ocr_progress(self, file_id: int) -> tuple[int, int]:
        """返回 (已 OCR 到第几页, 上次用到的上限) ✓"""
        try:
            r = self._query("SELECT ocr_pages, ocr_limit FROM files WHERE id=?",
                            (int(file_id),))
            if r:
                return (int(r[0]["ocr_pages"] or 0), int(r[0]["ocr_limit"] or 0))
        except Exception:                                        # noqa: BLE001
            pass
        return 0, 0

    def set_ocr_progress(self, file_id: int, pages: int, limit: int) -> None:
        """记下"已 OCR 到第几页"（pages）和"当时的上限"（limit）✓"""
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE files SET ocr_pages=?, ocr_limit=? WHERE id=?",
                    (int(pages), int(limit), int(file_id)))
                self._conn.commit()
        except Exception as exc:                                 # noqa: BLE001
            log.debug("记 OCR 进度失败（不影响入库）：%s", exc)

    # -------------------------- 单份 PDF 的页码范围（优先级最高，2026-10-19）
    # 项目主："应该是要某个特定 PDF 单独设页码范围，这个设置应该是最高确认，
    #          比如全局自动全部，个别取页码范围，以个别设置为准"
    def ocr_pages_of(self, file_id: int) -> tuple[int, int]:
        """这个 PDF 单独设的页码范围：(起, 止)。(0, 0) = 没单独设过、跟随全局 ✓"""
        try:
            r = self._query("SELECT ocr_from, ocr_to FROM files WHERE id=?",
                            (int(file_id),))
            if r:
                return (int(r[0]["ocr_from"] or 0), int(r[0]["ocr_to"] or 0))
        except Exception:                                        # noqa: BLE001
            pass
        return 0, 0

    def set_ocr_pages(self, file_id: int, page_from: int = 0,
                      page_to: int = 0) -> None:
        """给单份 PDF 设页码范围；(0, 0) = 清除、恢复跟随全局 ✓

        设了新范围就把**进度清零**（否则断点会挡住新范围）✓
        """
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE files SET ocr_from=?, ocr_to=?, ocr_pages=0 WHERE id=?",
                    (int(page_from), int(page_to), int(file_id)))
                self._conn.commit()
        except Exception as exc:                                 # noqa: BLE001
            log.warning("设单份 PDF 页码范围失败：%s", exc)

    # ================================================ 插图打标队列（2026-10-19）
    # 项目主选定的用法："每个文件夹单独设（三档）" + 白天只登记、夜里全力跑 ✓
    def caption_enqueue(self, file_id: int, folder_id: int,
                        items: list[dict]) -> int:
        """把"该打标的插图"登记进队列。返回**新登记**的条数。

        `items` 来自 app/pdfimages.py：{page, xref, sha1, width, height}
        · 同一文件同一张图（sha1 相同）只登记一次 ✓（重扫不会重复排队）
        · 已经做完（state=done）的也不重复登记 ✓
        """
        if not items:
            return 0
        added = 0
        now = time.time()
        with self._lock:
            for it in items:
                sha = str(it.get("sha1") or "")
                if not sha:
                    continue
                cur = self._conn.execute(
                    "SELECT id, state FROM caption_queue WHERE file_id=? AND sha1=?",
                    (int(file_id), sha))
                row = cur.fetchone()
                if row is not None:
                    # 已有：做完的保持做完；失败的重新排上（换模型/换网络该重试）✓
                    if row["state"] in ("done", "skip"):
                        continue
                    self._conn.execute(
                        "UPDATE caption_queue SET state='pending', updated=? WHERE id=?",
                        (now, int(row["id"])))
                    continue
                self._conn.execute(
                    "INSERT INTO caption_queue(file_id, folder_id, page, xref, sha1,"
                    " width, height, state, tries, created, updated)"
                    " VALUES(?,?,?,?,?,?,?,'pending',0,?,?)",
                    (int(file_id), int(folder_id), int(it.get("page") or 0),
                     int(it.get("xref") or 0), sha, int(it.get("width") or 0),
                     int(it.get("height") or 0), now, now))
                added += 1
            self._conn.commit()
        return added

    def caption_queue_list(self, *, state: str = "pending",
                           folder_ids: list[int] | None = None,
                           file_ids: list[int] | None = None,
                           limit: int = 200) -> list[dict]:
        """取队列（带文件名/路径，界面直接能显示 ✓）

        ★ 2026-10-20 下午：多两样东西
          · `file_ids` —— 只取这几份 PDF 的待办（项目主："需要指定文件"）
          · `file_caption_model` —— 这份文件自己的打标模型（空 = 跟随文件夹）✓
        """
        sql = ("SELECT q.*, f.name AS file_name, f.path AS file_path, "
               "       f.kind AS file_kind, f.caption_model AS file_caption_model "
               "FROM caption_queue q LEFT JOIN files f ON f.id = q.file_id "
               "WHERE q.state = ?")
        args: list = [str(state)]
        if folder_ids:
            marks = ",".join("?" for _ in folder_ids)
            sql += f" AND q.folder_id IN ({marks})"
            args.extend(int(x) for x in folder_ids)
        if file_ids:
            marks = ",".join("?" for _ in file_ids)
            sql += f" AND q.file_id IN ({marks})"
            args.extend(int(x) for x in file_ids)
        sql += " ORDER BY q.folder_id, q.file_id, q.page LIMIT ?"
        args.append(max(1, int(limit)))
        try:
            return [dict(r) for r in self._query(sql, tuple(args))]
        except Exception as exc:                                 # noqa: BLE001
            log.warning("读打标队列失败：%s", exc)
            return []

    def get_folder_privacy(self, folder_id: int) -> bool:
        """这个文件夹是不是「🔒 隐私文件夹」（内容永不出机器）✓"""
        try:
            r = self._query("SELECT IFNULL(privacy, 0) AS p FROM folders WHERE id=?",
                            (int(folder_id),))
            return bool(int(r[0]["p"] or 0)) if r else False
        except Exception:                                        # noqa: BLE001
            return False

    def set_folder_privacy(self, folder_id: int, on: bool) -> bool:
        """开关「🔒 隐私文件夹」✓

        项目主 2026-10-20："我的隐私文件统统放在里面，这样设置更方便"
        打开后：① 打标强制走本地视觉模型（图片不出机器）
                ② 问答用外部模型时，它的片段一条都不发出去
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE folders SET privacy=? WHERE id=?",
                (1 if on else 0, int(folder_id)))
            self._conn.commit()
            return cur.rowcount > 0


    def set_file_caption_model(self, file_id: int, model: str) -> bool:
        """设置某份文件的打标模型：'' （跟随文件夹）/ 'local' / 'remote' ✓"""
        value = str(model or "").strip().lower()
        if value not in ("", "local", "remote"):
            raise ValueError(f"不认识的打标模型档位：{model!r}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE files SET caption_model=? WHERE id=?",
                (value, int(file_id)))
            self._conn.commit()
            return cur.rowcount > 0

    def caption_queue_files(self, folder_ids: list[int] | None = None) -> list[dict]:
        """队列里"还有待办"的文件清单（界面选文件用）✓

        返回 [{file_id, name, folder_id, pending, file_caption_model, folder_model}]
        """
        sql = ("SELECT q.file_id, COUNT(*) AS pending, "
               "       MIN(f.name) AS name, MIN(f.caption_model) AS file_caption_model, "
               "       q.folder_id, MIN(fo.config) AS folder_config "
               "FROM caption_queue q "
               "LEFT JOIN files f ON f.id = q.file_id "
               "LEFT JOIN folders fo ON fo.id = q.folder_id "
               "WHERE q.state = 'pending'")
        args: list = []
        if folder_ids:
            marks = ",".join("?" for _ in folder_ids)
            sql += f" AND q.folder_id IN ({marks})"
            args.extend(int(x) for x in folder_ids)
        sql += " GROUP BY q.file_id, q.folder_id ORDER BY q.folder_id, name"
        try:
            return [dict(r) for r in self._query(sql, tuple(args))]
        except Exception as exc:                                 # noqa: BLE001
            log.warning("读待标文件清单失败：%s", exc)
            return []

    def caption_queue_mark(self, qid: int, state: str, note: str = "") -> None:
        """把一条队列项标成 done / failed / skip ✓（失败会累加 tries）"""
        with self._lock:
            self._conn.execute(
                "UPDATE caption_queue SET state=?, note=?, "
                " tries = tries + CASE WHEN ? = 'failed' THEN 1 ELSE 0 END,"
                " updated=? WHERE id=?",
                (str(state), str(note)[:300], str(state), time.time(), int(qid)))
            self._conn.commit()

    def caption_queue_stats(self) -> dict:
        """各状态的条数（界面显示"待打标 N 张"用 ✓）"""
        out = {"pending": 0, "done": 0, "failed": 0, "skip": 0}
        try:
            for r in self._query("SELECT state, COUNT(*) AS n FROM caption_queue "
                                 "GROUP BY state"):
                out[str(r["state"])] = int(r["n"])
        except Exception:                                        # noqa: BLE001
            pass
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out

    def caption_skip_clear(self) -> int:
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM caption_skip").fetchone()[0]
            self._conn.execute("DELETE FROM caption_skip")
            self._conn.commit()
        return int(n)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            # 批量期间由 batch() 统一提交；getattr 兜底，防止老实例没这个属性 ✗
            if not getattr(self, "_batching", False):
                self._conn.commit()
            return cur

    @contextlib.contextmanager
    def batch(self):
        """把一批写操作合成一个事务（索引器用）。

        ★ 2026-11-03：原来每条语句都 commit，扫 53 万个文件就是 53 万次 fsync，
          实测要几小时；包进一个事务 + 每几百个文件提交一次，快几十倍 ✓
          中途崩了最多丢最后一批（下次扫描会补上），不影响已提交的数据。
        """
        with self._lock:
            self._batching = True
        try:
            yield
        finally:
            with self._lock:
                self._batching = False
                self._conn.commit()

    def commit(self) -> None:
        """批量期间手动落盘一次（每 N 个文件调一次）。"""
        with self._lock:
            self._conn.commit()

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args))

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ------------------------------------------------------------ 界面偏好
    # 浏览器把界面偏好（折叠状态、上次标签页、视图模式…）存在 localStorage 里，
    # 但 stop.bat 用 taskkill /f 关浏览器窗口，Chrome 来不及落盘就被杀了，
    # 重启后设置全丢。所以服务端留一份，页面打开时回灌。
    # 存在 data/index/index.db 的 meta 表里，发布包不含 data\，不会带出去。
    _PREF_PREFIX = "ui_pref."

    # ★ 2026-10-20 多人版第一步：界面偏好**按用户存** ✓
    #   以前只有 meta 表一份（全局），两个人的「外部模型密钥 / 打标模型 /
    #   备份目录」会互相覆盖 ✗ —— 这不是"以后的需求"，是现在就存在的 bug
    #   做法：meta 里那份仍然当**全局默认**（后台任务、没登录时读它，行为不变），
    #        user_prefs 里存"每个人自己的覆盖"，读的时候叠上去 ✓
    # 敏感前缀：这些是"管理员的凭据"，普通用户**不该继承** ✗
    # 项目主 2026-10-21："外部模型设置，API默认也应该空白，不应该有管理员所填信息"
    # 含密钥、不该继承全局的偏好前缀（项目主 2026-10-21：Dify 的 key 也算）
    _SENSITIVE_PREFIXES = ("eg2-rm", "eg2-dify")

    def prefs_all(self, user_id: int = 0,
                  inherit_sensitive: bool = True) -> dict[str, str]:
        """全局默认 + 该用户的覆盖（user_id=0 只看全局）✓

        `inherit_sensitive=False`（普通用户）时：
          **`eg2-rm*` 这类凭据不继承全局**，只有他自己设过的那些才返回。
          否则管理员填的 API 密钥会被他读到 —— 那是安全事故 ✗

        ⚠ 2026-10-22：`eg2-dify*`（Dify 知识库的地址/库 ID/密钥）**一律不继承**，
        跟 `inherit_sensitive` 无关 —— 项目主："不应该是个人连接个人的吗？"
        以前本机上所有人都算"管理员"，于是全局那份 dify 配置会被所有人读到 ✗
        """
        out: dict[str, str] = {}
        for row in self._query("SELECT key, value FROM meta WHERE key LIKE ?",
                               (self._PREF_PREFIX + "%",)):
            out[row["key"][len(self._PREF_PREFIX):]] = row["value"]
        uid = int(user_id or 0)
        mine: list = []
        if uid > 0:
            try:
                mine = self._query(
                    "SELECT key, value FROM user_prefs WHERE user_id=?", (uid,))
            except Exception:                                    # noqa: BLE001
                mine = []
            mine_keys = set()
            for row in mine:
                out[str(row["key"])] = row["value"]
                mine_keys.add(str(row["key"]))
            if not inherit_sensitive:
                for k in list(out):
                    if k.startswith(self._SENSITIVE_PREFIXES) and k not in mine_keys:
                        del out[k]
        # Dify：永远只认"自己的那份"（没有就空），谁的凭据就归谁 ✓
        for k in list(out):
            if k.startswith("eg2-dify") and (uid <= 0 or k not in
                                            {str(r["key"]) for r in (mine or [])}):
                del out[k]
        return out

    def prefs_set(self, key: str, value: str, user_id: int = 0) -> None:
        uid = int(user_id or 0)
        if uid > 0:
            self._exec(
                "INSERT INTO user_prefs(user_id,key,value) VALUES(?,?,?) "
                "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
                (uid, str(key), str(value)))
            return
        self.set_meta(self._PREF_PREFIX + str(key), str(value))

    def prefs_delete(self, key: str, user_id: int = 0) -> None:
        uid = int(user_id or 0)
        if uid > 0:
            # 只删他自己的那份；**不动**全局默认（否则会把别人的默认一起清掉 ✗）
            self._exec("DELETE FROM user_prefs WHERE user_id=? AND key=?",
                       (uid, str(key)))
            return
        self._exec("DELETE FROM meta WHERE key=?", (self._PREF_PREFIX + str(key),))

    def check_dim(self) -> int:
        """记录全局默认维度。注意：各文件夹可以有自己的维度，
        换成不同的维度只影响以后新建/重建的文件夹，已有索引不会因此失效。"""
        dim = self.cfg.index_dim
        if self.get_meta("dim") is None:
            self.set_meta("dim", str(dim))
        return dim

    def reset(self) -> None:
        with self._lock:
            self._conn.executescript("DELETE FROM chunks; DELETE FROM files;")
            self._conn.commit()
        self._invalidate()

    # ------------------------------------------------------------ 文件夹
    def add_folder(self, path: str, config: dict | None = None,
                   category: str | None = None) -> int:
        """新增一个文件夹（category = 用途类别，空串 = 未分类 ✓）。

        ⚠ category 列可能还不存在（老库没迁移完 / 单人模式）→ 静默降级，
        只写 path+config ✓（结构变更由 app/multiuser.py::ensure_schema 负责）。
        """
        from .pathutil import safe_resolve

        path = str(safe_resolve(path))
        payload = json.dumps(config or {}, ensure_ascii=False)
        existing = self._query("SELECT id FROM folders WHERE path=?", (path,))
        if existing:
            return int(existing[0]["id"])
        cols = {r[1] for r in self._query("PRAGMA table_info(folders)")}
        if category is not None and "category" in cols:
            cur = self._exec(
                "INSERT INTO folders(path, added_at, config, category) VALUES(?,?,?,?)",
                (path, time.time(), payload, str(category or "")))
        else:
            cur = self._exec("INSERT INTO folders(path, added_at, config) VALUES(?,?,?)",
                             (path, time.time(), payload))
        return int(cur.lastrowid)

    def set_folder_category(self, folder_id: int, category: str) -> None:
        cols = {r[1] for r in self._query("PRAGMA table_info(folders)")}
        if "category" not in cols:
            return
        self._exec("UPDATE folders SET category=? WHERE id=?",
                   (str(category or ""), int(folder_id)))

    def set_folder_config(self, folder_id: int, config: dict) -> None:
        self._exec("UPDATE folders SET config=? WHERE id=?",
                   (json.dumps(config or {}, ensure_ascii=False), folder_id))

    def set_folder_owner(self, folder_id: int, owner_id: int,
                         index_state: str | None = None) -> None:
        """改归属（多人）：普通人自己加的文件夹必须 owner_id=自己 ✓。

        老库里 folders 没有这两列时静默跳过（单人模式不关心）—— 迁移由
        app/multiuser.py::ensure_schema 负责，这里只写值，不做结构变更。
        """
        cols = {r[1] for r in self._query("PRAGMA table_info(folders)")}
        if "owner_id" not in cols:
            return
        if index_state is not None and "index_state" in cols:
            self._exec("UPDATE folders SET owner_id=?, index_state=? WHERE id=?",
                       (int(owner_id), str(index_state), int(folder_id)))
        else:
            self._exec("UPDATE folders SET owner_id=? WHERE id=?",
                       (int(owner_id), int(folder_id)))

    def get_folder(self, folder_id: int) -> dict | None:
        rows = self._query("SELECT * FROM folders WHERE id=?", (folder_id,))
        if not rows:
            return None
        item = dict(rows[0])
        item["config"] = _loads(item.get("config"))
        return item

    def list_folders(self, stats: bool = True,
                     check_exists: bool = True) -> list[dict[str, Any]]:
        """列出文件夹。

        `check_exists=False`：**不碰网络盘**，只用已知缓存（未知就是 None）。
        ★ 2026-11-03：界面一打开就调这个接口，以前它对每个文件夹都做
          `os.path.isdir`；NAS 卡一下就要几秒，页面就一直转「正在读取文件夹…」✗
          现在首屏一律走缓存，真正的检查交给「🔄 重新检查目录」✓
        """
        if stats:
            # ★ 2026-11-03：原来是"每个文件夹 3 个关联子查询"，68 万文件 / 2.3GB 的库上
            #   实测 /api/folders 要 70~80 秒 → 页面一直转「正在读取文件夹…」✗
            #   改成：一次 GROUP BY 拿全部计数（再缓存 30 秒），毫秒级 ✓
            rows = self._query("SELECT * FROM folders ORDER BY added_at")
            cached = getattr(self, "_folder_counts", None)
            _now = time.time()
            if cached and _now - cached[0] < 30:
                fmap, cmap = cached[1], cached[2]
            else:
                # ★ 2026-11-03：先从 meta 里读上次算好的（重启后首屏也不用等）✓
                try:
                    raw = self.get_meta("folder_counts_json")
                    if raw:
                        saved = json.loads(raw)
                        fmap = {int(k): tuple(v) for k, v in (saved.get("f") or {}).items()}
                        cmap = {int(k): int(v) for k, v in (saved.get("c") or {}).items()}
                        self._folder_counts = (_now, fmap, cmap)
                        rows_ready = True
                    else:
                        rows_ready = False
                except Exception:                                # noqa: BLE001
                    rows_ready = False
                if not rows_ready:
                    fmap = {int(r["folder_id"]): (int(r["c"]), int(r["e"]))
                            for r in self._query(
                                "SELECT folder_id, COUNT(*) AS c, "
                                "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS e "
                                "FROM files GROUP BY folder_id")}
                    cmap = {int(r["folder_id"]): int(r["c"]) for r in self._query(
                        "SELECT x.folder_id AS folder_id, COUNT(*) AS c "
                        "FROM chunks c JOIN files x ON x.id=c.file_id GROUP BY x.folder_id")}
                    self._folder_counts = (_now, fmap, cmap)
                    try:
                        self.set_meta("folder_counts_json", json.dumps(
                            {"f": {str(k): list(v) for k, v in fmap.items()},
                             "c": {str(k): v for k, v in cmap.items()}}))
                    except Exception:                            # noqa: BLE001
                        pass
        else:
            rows = self._query("SELECT * FROM folders f ORDER BY f.added_at")
            fmap, cmap = {}, {}
        out = []
        for row in rows:
            item = dict(row)
            if stats:
                _f, _e = fmap.get(int(item["id"]), (0, 0))
                item["files"] = int(_f)
                item["errors"] = int(_e)
                item["chunks"] = int(cmap.get(int(item["id"]), 0))
            item["config"] = _loads(item.get("config"))
            try:
                if check_exists:
                    item["exists"] = path_exists_cached(str(row["path"]))
                else:
                    hit = _exists_cache.get(str(row["path"]))
                    item["exists"] = None if hit is None else bool(hit[1])
            except OSError:
                item["exists"] = False
            out.append(item)
        return out

    def remove_folder(self, folder_id: int) -> None:
        ids = [r["id"] for r in self._query("SELECT id FROM files WHERE folder_id=?", (folder_id,))]
        with self._lock:
            if ids:
                marks = ",".join("?" * len(ids))
                self._conn.execute(f"DELETE FROM chunks WHERE file_id IN ({marks})", ids)
            self._conn.execute("DELETE FROM files WHERE folder_id=?", (folder_id,))
            self._conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
            self._conn.commit()
        self._invalidate()

    def set_folder_scan(self, folder_id: int, status: str = "idle") -> None:
        self._exec("UPDATE folders SET last_scan=?, status=? WHERE id=?",
                   (time.time(), status, folder_id))

    # ---------------- 目录 mtime 缓存（多人 ⑥ 索引加速）----------------
    def dir_mtimes(self, folder_id: int) -> dict[str, tuple[float, int]]:
        """上次扫描时各目录的 (mtime, 子目录个数)（表不存在/没扫过 → 空字典）✓"""
        try:
            rows = self._query("SELECT path, mtime, n_dirs FROM dir_scan WHERE folder_id=?",
                               (int(folder_id),))
            return {str(r["path"]): (float(r["mtime"]), int(r["n_dirs"] or 0))
                    for r in rows}
        except sqlite3.Error:
            return {}

    def save_dir_mtimes(self, folder_id: int,
                        mtimes: dict[str, tuple[float, int]]) -> int:
        """把这次扫到的目录 (mtime, 子目录个数) 记下来（下次叶子目录没变就跳过 ✓）。

        只 upsert 见到的目录；**不删除**没见到的 —— 目录可能只是这次没扫到
        （共享掉线之类），删了下次就得重建缓存 ✗。
        """
        if not mtimes:
            return 0
        rows = [(int(folder_id), str(p), float(m), int(n))
                for p, (m, n) in mtimes.items()]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO dir_scan(folder_id, path, mtime, n_dirs) "
                "VALUES(?,?,?,?)", rows)
            self._conn.commit()
        return len(rows)

    # ------------------------------------------------------------ 文件
    def get_files_map(self, folder_id: int) -> dict[str, sqlite3.Row]:
        rows = self._query("SELECT * FROM files WHERE folder_id=?", (folder_id,))
        return {row["path"]: row for row in rows}

    # ------------------------- 服务器路径映射（2026-10-20，项目主要求）------------
    # "我桌面版用了 \\NAS\photo\家庭相册，它应该旁边有个服务器对应的路径可以自定义"
    # 数据库两边共享：`path` 是本机视角（权威键），`server_path` 是服务器视角 ✓
    def set_folder_server_path(self, folder_id: int, server_path: str) -> None:
        """给文件夹设"它在服务器上的路径"；传空字符串 = 清除（回到自动建议）✓"""
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE folders SET server_path=? WHERE id=?",
                    (str(server_path or "")[:1000], int(folder_id)))
                self._conn.commit()
        except Exception as exc:                                 # noqa: BLE001
            log.warning("设服务器路径失败：%s", exc)

    def folder_server_path(self, folder_id: int) -> str:
        """读这个文件夹的服务器路径；没设过返回空串 ✓"""
        try:
            r = self._query("SELECT server_path FROM folders WHERE id=?",
                            (int(folder_id),))
            if r:
                return str(r[0]["server_path"] or "")
        except Exception:                                        # noqa: BLE001
            pass
        return ""

    # ------------------------- 本机路径别名（2026-10-20，项目主追加）-------------
    # "本机的文件夹也需可以自定义设置同步目录"
    # 为什么不直接改 folders.path：那是 files.path 的前缀，改了会牵连整个索引 ✗
    # 用 local_alias 更安全：扫描/预览时"实际去哪个目录找"，engine 里取 alias or path ✓
    def set_folder_local_alias(self, folder_id: int, local_alias: str) -> None:
        """给文件夹设"本机实际目录"；空字符串 = 清除（就用 path）✓"""
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE folders SET local_alias=? WHERE id=?",
                    (str(local_alias or "")[:1000], int(folder_id)))
                self._conn.commit()
        except Exception as exc:                                 # noqa: BLE001
            log.warning("设本机路径别名失败：%s", exc)

    def folder_local_path(self, folder_id: int) -> str:
        """这个文件夹**本机实际该去哪个目录找**：local_alias 优先，否则 path ✓"""
        try:
            r = self._query("SELECT path, local_alias FROM folders WHERE id=?",
                            (int(folder_id),))
            if r:
                return str(r[0]["local_alias"] or r[0]["path"] or "")
        except Exception:                                        # noqa: BLE001
            pass
        return ""

    def folder_paths(self, folder_id: int) -> dict:
        """一次给出三个路径（界面显示 / 服务器转换用）✓
        返回 {"path": 权威键, "local": 本机实际, "server": 服务器实际}
        """
        out = {"path": "", "local": "", "server": ""}
        try:
            r = self._query("SELECT path, local_alias, server_path FROM folders "
                            "WHERE id=?", (int(folder_id),))
            if r:
                out["path"] = str(r[0]["path"] or "")
                out["local"] = str(r[0]["local_alias"] or r[0]["path"] or "")
                out["server"] = str(r[0]["server_path"] or "")
        except Exception:                                        # noqa: BLE001
            pass
        return out
        return {row["path"]: row for row in rows}

    def upsert_file(self, folder_id: int, path: Path, size: int, mtime: float,
                    kind: str, note: str = "") -> int:
        p = str(path)
        with self._lock:
            row = self._conn.execute("SELECT id FROM files WHERE path=?", (p,)).fetchone()
            now = time.time()
            if row:
                file_id = int(row["id"])
                self._conn.execute(
                    "UPDATE files SET folder_id=?, size=?, mtime=?, kind=?, indexed_at=?, "
                    "status='ok', note=? WHERE id=?",
                    (folder_id, size, mtime, kind, now, note, file_id),
                )
            else:
                cur = self._conn.execute(
                    "INSERT INTO files(folder_id, path, dir, name, ext, size, mtime, kind, "
                    "indexed_at, status, note) VALUES(?,?,?,?,?,?,?,?,?,'ok',?)",
                    (folder_id, p, str(path.parent), path.name, path.suffix.lower(),
                     size, mtime, kind, now, note),
                )
                file_id = int(cur.lastrowid)
            # ★ 2026-11-03：批量期间不逐条提交。原来这里无视 _batching，
            #   每个文件一次 fsync，实测只有 30 条/秒 —— 53 万文件要 5 小时 ✗
            if not getattr(self, "_batching", False):
                self._conn.commit()
        self._invalidate()
        return file_id

    def set_file_error(self, file_id: int | None, folder_id: int, path: Path,
                       message: str = "索引失败") -> None:
        if file_id is None:
            try:
                stat = path.stat()
                file_id = self.upsert_file(folder_id, path, stat.st_size, stat.st_mtime, "other")
            except OSError:
                return
        self._exec("UPDATE files SET status='error', note=? WHERE id=?", (message[:300], file_id))

    def mark_ok(self, file_id: int, note: str = "") -> None:
        self._exec("UPDATE files SET status='ok', note=? WHERE id=?", (note[:300], file_id))

    def delete_paths(self, paths: list[str]) -> int:
        if not paths:
            return 0
        with self._lock:
            total = 0
            for i in range(0, len(paths), 500):
                batch = paths[i:i + 500]
                marks = ",".join("?" * len(batch))
                ids = [r["id"] for r in self._conn.execute(
                    f"SELECT id FROM files WHERE path IN ({marks})", batch)]
                if ids:
                    idmarks = ",".join("?" * len(ids))
                    self._conn.execute(f"DELETE FROM chunks WHERE file_id IN ({idmarks})", ids)
                    self._conn.execute(f"DELETE FROM files WHERE id IN ({idmarks})", ids)
                    total += len(ids)
            self._conn.commit()
        self._invalidate()
        return total

    def clear_file_chunks(self, file_id: int, keep_caption: bool = False) -> None:
        """清掉这个文件的片段。

        `keep_caption=True` 时**保留 caption 片段**（打标结果）✓ ——
        项目主 2026-10-18：重建索引会把辛苦打好的图片描述一起清掉 ✗（那很贵），
        所以重建只重做内容/图片向量，不动已经打好的描述 ✓。
        """
        with self._lock:
            if keep_caption:
                self._conn.execute(
                    "DELETE FROM chunks WHERE file_id=? AND kind<>'caption'",
                    (file_id,))
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE file_id=?", (file_id,)).fetchone()
                left = int(row[0]) if row else 0
                self._conn.execute("UPDATE files SET n_chunks=? WHERE id=?",
                                   (left, file_id))
            else:
                self._conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
                self._conn.execute("UPDATE files SET n_chunks=0 WHERE id=?", (file_id,))
            self._conn.commit()
        self._invalidate()

    def add_chunks(self, file_id: int, items: list[tuple[str, Any, str]]) -> None:
        """items: [(文本, 向量, 类型)]

        顺便写 `uni`（逐字切分的正文）—— FTS5 那边靠触发器自动同步 ✓
        （见 app/ftsindex.py：中文不切字的话两字查询一条都命中不了 ✗）
        """
        if not items:
            return
        from . import ftsindex as _fts                            # noqa: PLC0415

        rows = [(file_id, seq, text, _to_blob(vec), len(vec), kind, _fts.unigrams(text))
                for seq, (text, vec, kind) in enumerate(items)]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO chunks(file_id, seq, text, vec, dim, kind, uni) "
                "VALUES(?,?,?,?,?,?,?)", rows)
            self._conn.execute("UPDATE files SET n_chunks=? WHERE id=?", (len(rows), file_id))
            self._conn.commit()
        self._invalidate()

    def set_name_vec(self, file_id: int, vector: Any) -> None:
        self.set_name_vecs([(file_id, vector)])

    def set_name_vecs(self, items: list[tuple[int, Any]]) -> None:
        """批量写"文件名向量"（一次事务）。

        ★ 2026-11-03 实测：`仅文件名` 模式扫到 53 万个文件时，原来每个文件
          一次 UPDATE + 一次 commit（fsync），跑到几小时还没完 ✗
          → 改成 executemany + 单事务，几千个一批，速度差几十倍 ✓
        """
        if not items:
            return
        rows = []
        for file_id, vector in items:
            vec = np.asarray(vector, dtype=np.float32).reshape(-1)
            rows.append((_to_blob(vec), int(vec.shape[0]), int(file_id)))
        with self._lock:
            self._conn.executemany(
                "UPDATE files SET name_vec=?, name_dim=? WHERE id=?", rows)
            self._conn.commit()

    # ------------------------------------------------------------ 统计
    def warm_stats(self) -> None:
        """后台预热统计缓存（全站口径）。界面请求就只读缓存，不再等聚合查询 ✓"""
        try:
            self.stats(None)
        except Exception:                                        # noqa: BLE001
            pass

    def warm_stats_json(self) -> dict[str, Any]:
        """算一次全站统计，存到 meta + 内存（后台线程调用）✓"""
        try:
            data = self.stats(None)
        except Exception:                                        # noqa: BLE001
            return {}
        self._stats_mem = data
        try:
            self.set_meta("stats_json", json.dumps(data, ensure_ascii=False))
        except Exception:                                        # noqa: BLE001
            pass
        return data

    def stats_fast(self) -> dict[str, Any]:
        """**永不查询**的统计：内存 → meta → 全 0（毫秒级）✓

        ★ 2026-11-03：界面一打开就要统计，而这个库上真算一次要 25~70 秒。
          先给上次的结果（最多差 90 秒），后台线程负责刷新。
        """
        mem = getattr(self, "_stats_mem", None)
        if mem:
            return dict(mem)
        try:
            raw = self.get_meta("stats_json")
            if raw:
                data = json.loads(raw)
                self._stats_mem = data
                return dict(data)
        except Exception:                                        # noqa: BLE001
            pass
        return {"files": 0, "chunks": 0, "errors": 0, "bytes": 0,
                "kinds": {}, "dim": self.get_meta("dim"),
                "scoped": False, "warming": True}

    def stats(self, folder_ids: list[int] | None = None) -> dict[str, Any]:
        """索引统计。

        ★ 2026-10-21 项目主：“用户登陆，这里不应该显示整站全局信息，
          只能显示用户文件信息” —— 之前无参数、一律全库 ✗
          older_ids=None  = 全站（管理员 / 未登录）
          older_ids=[...] = 只算这些文件夹（普通用户只看自己能用的）
          older_ids=[]    = 什么都看不到 → 全 0（不能退化成全站 ✗）
        """
        # ★ 2026-11-03：结果缓存 15 秒。
        #   界面和监控每隔几秒轮询 /api/index/status，每次都跑 5 个聚合查询；
        #   在 689k 行 / 2GB 的库上每次 1~2 秒，还和索引线程抢同一把库锁
        #   → 索引被拖到 30 个/秒以下（实测）。缓存后索引不再被轮询饿死 ✓
        _key = None if folder_ids is None else tuple(int(x) for x in folder_ids)
        _now = time.time()
        _cache = getattr(self, "_stats_cache", None)
        if _cache and _now - _cache[0] < 15 and _cache[1] == _key:
            return dict(_cache[2])

        if folder_ids is None:
            files = self._query("SELECT COUNT(*) c FROM files")[0]["c"]
            chunks = self._query("SELECT COUNT(*) c FROM chunks")[0]["c"]
            errors = self._query(
                "SELECT COUNT(*) c FROM files WHERE status='error'")[0]["c"]
            size = self._query("SELECT COALESCE(SUM(size),0) s FROM files")[0]["s"]
            kinds = {row["kind"] or "other": row["c"] for row in self._query(
                "SELECT kind, COUNT(*) c FROM files GROUP BY kind ORDER BY c DESC")}
        elif not folder_ids:
            files = chunks = errors = 0
            size = 0
            kinds = {}
        else:
            marks = ",".join("?" * len(folder_ids))
            args = tuple(int(x) for x in folder_ids)
            files = self._query(
                f"SELECT COUNT(*) c FROM files WHERE folder_id IN ({marks})", args)[0]["c"]
            chunks = self._query(
                "SELECT COUNT(*) c FROM chunks ch JOIN files f ON f.id=ch.file_id "
                f"WHERE f.folder_id IN ({marks})", args)[0]["c"]
            errors = self._query(
                "SELECT COUNT(*) c FROM files WHERE status='error' "
                f"AND folder_id IN ({marks})", args)[0]["c"]
            size = self._query(
                f"SELECT COALESCE(SUM(size),0) s FROM files WHERE folder_id IN ({marks})",
                args)[0]["s"]
            kinds = {row["kind"] or "other": row["c"] for row in self._query(
                "SELECT kind, COUNT(*) c FROM files "
                f"WHERE folder_id IN ({marks}) GROUP BY kind ORDER BY c DESC", args)}
        out = {"files": files, "chunks": chunks, "errors": errors,
               "bytes": size, "kinds": kinds, "dim": self.get_meta("dim"),
               "scoped": folder_ids is not None}
        self._stats_cache = (_now, _key, dict(out))
        return out

    # ------------------------------------------------------------ 向量缓存
    def _invalidate(self) -> None:
        self._cache = None

    _cache: dict | None = None

    def _load_matrix(self) -> dict:
        """把索引里的向量按维度分组载入内存。

        不同文件夹可以选不同的向量维度。Matryoshka 表示下「取前 d 维再重新归一化」
        就等于该维度本身的结果，所以只需把查询向量按各分组的维度切片，跨维度也能一起比。
        """
        version = self._query(
            "SELECT (SELECT COUNT(*) FROM chunks) AS c, "
            "(SELECT COALESCE(MAX(id),0) FROM chunks) AS m, "
            "(SELECT COUNT(*) FROM files) AS f")[0]
        key = (version["c"], version["m"], version["f"])
        cache = self._cache
        if cache and cache["key"] == key:
            return cache

        chunks: dict[int, dict] = {}
        for row in self._query(
                "SELECT id, file_id, vec, dim, kind FROM chunks"):
            dim = int(row["dim"])
            group = chunks.setdefault(
                dim, {"ids": [], "files": [], "blobs": [], "kinds": []})
            group["ids"].append(row["id"])
            group["files"].append(row["file_id"])
            group["blobs"].append(row["vec"])
            group["kinds"].append(row["kind"] or "")
        for group in chunks.values():
            group["vectors"] = np.vstack([_from_blob(b) for b in group["blobs"]])
            group["file_ids"] = np.asarray(group["files"], dtype=np.int64)
            group["chunk_ids"] = np.asarray(group["ids"], dtype=np.int64)
            group["kind_arr"] = np.asarray(group["kinds"], dtype=object)
            for extra in ("ids", "files", "blobs", "kinds"):
                group.pop(extra, None)

        names: dict[int, dict] = {}
        for row in self._query("SELECT id, name_vec, name_dim FROM files "
                               "WHERE name_vec IS NOT NULL"):
            dim = int(row["name_dim"] or len(_from_blob(row["name_vec"])))
            group = names.setdefault(dim, {"ids": [], "blobs": []})
            group["ids"].append(row["id"])
            group["blobs"].append(row["name_vec"])
        for group in names.values():
            group["vectors"] = np.vstack([_from_blob(b) for b in group["blobs"]])
            group["file_ids"] = np.asarray(group["ids"], dtype=np.int64)
            for extra in ("ids", "blobs"):
                group.pop(extra, None)

        cache = {"key": key, "chunks": chunks, "names": names}
        self._cache = cache
        return cache

    def get_file(self, file_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM files WHERE id=?", (file_id,))
        return dict(rows[0]) if rows else None

    # ------------------------------------------------------------ 检索
    def search(self, query_vec, top_k: int = 30,
               folder_ids: list[int] | None = None,
               exts: list[str] | None = None,
               literal: str = "",
               kinds: tuple | None = None,
               exclude_kinds: tuple | None = None) -> list[dict[str, Any]]:
        cache = self._load_matrix()
        query = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        scores: dict[int, tuple[float, int | None]] = {}

        for dim, group in cache["chunks"].items():
            probe = _slice_normalize(query, dim)
            if probe is None or not len(group["vectors"]):
                continue
            sims = group["vectors"] @ probe
            order = np.argsort(-sims)[: max(top_k * 40, 800)]
            ka = group.get("kind_arr")
            for idx in order:
                if ka is not None:
                    k = ka[idx]
                    if kinds and k not in kinds:
                        continue
                    # 黑名单优先：只排除图片这类没有文字的片段
                    if exclude_kinds and k in exclude_kinds:
                        continue
                fid = int(group["file_ids"][idx])
                score = float(sims[idx])
                best = scores.get(fid)
                if best is None or score > best[0]:
                    scores[fid] = (score, int(group["chunk_ids"][idx]))

        for dim, group in cache["names"].items():
            probe = _slice_normalize(query, dim)
            if probe is None or not len(group["vectors"]):
                continue
            sims = group["vectors"] @ probe
            order = np.argsort(-sims)[: max(top_k * 20, 400)]
            for idx in order:
                fid = int(group["file_ids"][idx])
                score = float(sims[idx]) + 0.02       # 文件名略微加权
                best = scores.get(fid)
                if best is None or score > best[0]:
                    scores[fid] = (score, best[1] if best else None)

        if not scores:
            return []

        file_ids = list(scores.keys())
        placeholders = ",".join("?" * len(file_ids))
        meta = {row["id"]: row for row in self._query(
            f"SELECT * FROM files WHERE id IN ({placeholders})", file_ids)}

        needle = literal.strip().lower()
        results: list[dict[str, Any]] = []
        for fid, (score, chunk_id) in scores.items():
            row = meta.get(fid)
            if row is None:
                continue
            if folder_ids and row["folder_id"] not in folder_ids:
                continue
            if exts and (row["ext"] or "") not in exts:
                continue
            name_lower = row["name"].lower()
            path_lower = row["path"].lower()
            if needle:
                if needle in name_lower:
                    score += 0.35
                elif needle in path_lower:
                    score += 0.15
                elif all(tok in path_lower for tok in needle.split() if tok):
                    score += 0.10
            snippet = ""
            if chunk_id is not None:
                got = self._query("SELECT text FROM chunks WHERE id=?", (chunk_id,))
                snippet = got[0]["text"] if got else ""
            results.append({
                "id": fid,
                "folder_id": row["folder_id"],
                "path": row["path"],
                "name": row["name"],
                "dir": row["dir"],
                "ext": row["ext"],
                "kind": row["kind"],
                "size": row["size"],
                "mtime": row["mtime"],
                "status": row["status"],
                "note": row["note"],
                "n_chunks": row["n_chunks"],
                "score": round(score, 4),
                "snippet": (snippet or "")[:600],
            })

        results.sort(key=lambda item: -item["score"])
        return results[:top_k]

    # ================= 人脸 / 人物 =================
    def clear_file_faces(self, file_id: int) -> None:
        self._exec("DELETE FROM faces WHERE file_id=?", (file_id,))

    def add_faces(self, file_id: int, items) -> None:
        """items: [(box, prob, vector), ...]"""
        if not items:
            return
        rows = [(file_id, float(b[0]), float(b[1]), float(b[2]), float(b[3]),
                 float(p), _to_blob(v), int(len(v))) for b, p, v in items]
        with self._conn:
            self._conn.executemany(
                "INSERT INTO faces(file_id,x1,y1,x2,y2,prob,vec,dim) "
                "VALUES(?,?,?,?,?,?,?,?)", rows)

    def load_face_matrix(self) -> dict:
        """按维度分组的人脸特征矩阵，用于快速比对。"""
        out: dict[int, dict] = {}
        for row in self._query("SELECT id, file_id, vec, dim FROM faces"):
            g = out.setdefault(int(row["dim"]),
                               {"vectors": [], "face_ids": [], "file_ids": []})
            g["vectors"].append(_from_blob(row["vec"]))
            g["face_ids"].append(int(row["id"]))
            g["file_ids"].append(int(row["file_id"]))
        for g in out.values():
            if g["vectors"]:
                m = np.stack(g["vectors"]).astype(np.float32)
                g["vectors"] = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        return out

    def search_faces(self, vec, top_k: int = 200, threshold: float = 0.45,
                     exclude_file_ids=None) -> list[dict]:
        """拿一个特征去找相似的人脸 —— 「以图搜人」的核心。"""
        q = np.asarray(vec, dtype=np.float32).reshape(-1)
        q = q / (np.linalg.norm(q) + 1e-9)
        skip = set(exclude_file_ids or ())
        hits: list[dict] = []
        for dim, g in self.load_face_matrix().items():
            probe = _slice_normalize(q, dim)
            if probe is None or not len(g["vectors"]):
                continue
            sims = g["vectors"] @ probe
            for i in np.where(sims >= threshold)[0]:
                fid = int(g["file_ids"][i])
                if fid in skip:
                    continue
                hits.append({"face_id": int(g["face_ids"][i]), "file_id": fid,
                             "sim": float(sims[i])})
        hits.sort(key=lambda h: -h["sim"])
        return hits[:top_k]

    def list_people(self, stats: bool = True) -> list[dict]:
        rows = self._query("SELECT * FROM people ORDER BY n_faces DESC, id")
        out = []
        for r in rows:
            item = {"id": int(r["id"]), "name": r["name"],
                    "kind": r["kind"], "n_faces": int(r["n_faces"] or 0),
                    "cover_face": r["cover_face"]}
            if stats:
                got = self._query(
                    "SELECT COUNT(DISTINCT file_id) n FROM faces WHERE person_id=?",
                    (item["id"],))
                item["n_files"] = int(got[0]["n"]) if got else 0
            out.append(item)
        return out

    def set_person_name(self, person_id: int, name: str,
                        kind: str | None = None) -> None:
        if kind:
            self._exec("UPDATE people SET name=?, kind=? WHERE id=?",
                       (name.strip(), kind, person_id))
        else:
            self._exec("UPDATE people SET name=? WHERE id=?", (name.strip(), person_id))

    def assign_people(self, threshold: float = 0.55, prune: float = 0.40) -> dict:
        """按「已命名的人」的人脸中心，把每张脸归类。

        不够像的脸保持 NULL（界面上显示为「未知」）。
        顺带做离群清理：组内离中心太远的脸踢回 NULL。
        """
        people = [p for p in self.list_people(stats=False)
                  if p["name"].strip() and p["kind"] != "object"]
        if not people:
            return {"assigned": 0, "unknown": 0, "people": 0}
        cache = self.load_face_matrix()
        pids = [p["id"] for p in people]
        ph = ",".join("?" * len(pids))
        rows = self._query(f"SELECT id, person_id, vec FROM faces "
                           f"WHERE person_id IN ({ph})", pids)
        per: dict[int, list] = {}
        for r in rows:
            per.setdefault(int(r["person_id"]), []).append(r)

        centers, cmap = [], []
        for pid in pids:
            items = per.get(pid, [])
            if not items:
                continue
            vecs = np.stack([_from_blob(r["vec"]) for r in items]).astype(np.float32)
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
            ids = [int(r["id"]) for r in items]
            if prune > 0 and len(ids) > 2:
                keep = np.ones(len(ids), dtype=bool)
                for _ in range(3):
                    c = vecs[keep].mean(axis=0)
                    c /= np.linalg.norm(c) + 1e-9
                    k2 = (vecs @ c) >= prune
                    if k2.all() or k2.sum() < 2:
                        break
                    keep = k2
                dropped = [i for i, k in zip(ids, keep) if not k]
                if dropped:
                    with self._conn:
                        self._conn.executemany(
                            "UPDATE faces SET person_id=NULL WHERE id=?",
                            [(i,) for i in dropped])
                    ids = [i for i, k in zip(ids, keep) if k]
                    vecs = vecs[keep]
            if not len(ids):
                continue
            c = vecs.mean(axis=0)
            c /= np.linalg.norm(c) + 1e-9
            centers.append(c)
            cmap.append(pid)
            self._exec("UPDATE people SET n_faces=?, cover_face=? WHERE id=?",
                       (len(ids), ids[0], pid))
        if not centers:
            return {"assigned": 0, "unknown": 0, "people": 0}

        C = np.stack(centers).astype(np.float32)
        assigned = unknown = 0
        updates = []
        for dim, g in cache.items():
            idx = [i for i, v in enumerate(g["vectors"]) if len(v) == C.shape[1]]
            if not idx:
                continue
            V = np.stack([g["vectors"][i] for i in idx])
            sims = V @ C.T
            best = sims.argmax(axis=1)
            for k, i in enumerate(idx):
                fid = int(g["face_ids"][i])
                if sims[k, best[k]] >= threshold:
                    updates.append((cmap[int(best[k])], fid))
                    assigned += 1
                else:
                    updates.append((None, fid))
                    unknown += 1
        if updates:
            with self._conn:
                self._conn.executemany("UPDATE faces SET person_id=? WHERE id=?", updates)        # 分配完成后重算一遍计数和封面。
        # 上面那次 UPDATE people SET n_faces 是在**分配之前**做的，
        # 分配完就过期了 —— 会出现"记录 711 张、实际 2298 张"
        # 以及一堆 n_faces>0 却没有任何脸的幽灵组。
        with self._conn:
            self._conn.execute(
                "UPDATE people SET n_faces=(SELECT COUNT(*) FROM faces "
                "WHERE person_id = people.id)")
            self._conn.execute(
                "UPDATE people SET cover_face=(SELECT id FROM faces "
                "WHERE person_id = people.id "
                "ORDER BY (prob*(x2-x1)*(y2-y1)) DESC LIMIT 1) "
                "WHERE n_faces > 0")
            self._conn.execute("DELETE FROM people WHERE n_faces = 0")


        return {"assigned": assigned, "unknown": unknown, "people": len(cmap)}

    def rebuild_people(self, threshold: float = 0.65, method: str = "average") -> dict:
        """把所有脸重新聚类成「人物」，共用户命名。

        参数取 face-lab 在 3000+ 张真实人脸上的实测结论：average-linkage + 0.65。
        已有名字会按「新组里多数脸的旧归属」沿用，不白费之前的命名。
        """
        from collections import Counter

        from scipy.cluster.hierarchy import fcluster, linkage
        from scipy.spatial.distance import pdist

        cache = self.load_face_matrix()
        if not cache:
            return {"groups": 0, "faces": 0}
        dim = max(cache, key=lambda d: len(cache[d]["vectors"]))
        g = cache[dim]
        V, ids = g["vectors"], g["face_ids"]

        tree = linkage(pdist(V, metric="cosine"), method=method)
        labels = fcluster(tree, t=1.0 - threshold, criterion="distance").astype(np.int32)

        old = {int(r["id"]): int(r["person_id"]) for r in self._query(
            "SELECT id, person_id FROM faces WHERE person_id IS NOT NULL")}
        pname = {int(p["id"]): (p["name"], p["kind"])
                 for p in self.list_people(stats=False)}
        # 快照：哪张脸原来叫什么名字。
        # 这是用户手工整理的成果，聚类结束后要**原样保住**，
        # 不能只靠"新组里 60% 同源"这种条件去猜。
        snap = {int(r["id"]): r["name"] for r in self._query(
            "SELECT f.id, p.name FROM faces f JOIN people p ON p.id = f.person_id "
            "WHERE p.name IS NOT NULL AND p.name <> ''")}

        made = 0
        with self._conn:
            self._conn.execute("UPDATE faces SET person_id=NULL")
            self._conn.execute("DELETE FROM people")
            self._conn.execute(
                "DELETE FROM sqlite_sequence WHERE name='people'")
            for lab in np.unique(labels):
                members = [int(ids[i]) for i in np.where(labels == lab)[0]]
                if not members:
                    continue
                c = Counter(old[m] for m in members if m in old)
                name, kind = "", "person"
                if c:
                    top_pid, cnt = c.most_common(1)[0]
                    if cnt / len(members) >= 0.6 and top_pid in pname:
                        name, kind = pname[top_pid]
                cur = self._conn.execute(
                    "INSERT INTO people(name, kind, n_faces, cover_face, created) "
                    "VALUES(?,?,?,?,?)",
                    (name, kind, len(members), members[0], time.time()))
                pid = cur.lastrowid
                self._conn.executemany("UPDATE faces SET person_id=? WHERE id=?",
                                       [(pid, m) for m in members])
                made += 1

            # 注意：这里**故意不做**同名去重。
            # 同一批脸按不同阈值聚类，同一个人被拆成几组是正常的中间状态 ——
            # 用户要逐组检查（改名的改名、剔除的剔除），最后再一键合并同名。
            # 自动合并会把这个过程挡掉。
            # ---- 把用户手工命名的脸强制归回同名人物 ----
            # 聚类的作用只是给"还没命名的脸"分组；
            # 已标注的归属是用户的劳动成果，必须原样保留。
            restored = 0
            if snap:
                pid_of: dict[str, int] = {}
                for nm in set(snap.values()):
                    row = self._conn.execute(
                        "SELECT id FROM people WHERE name=? LIMIT 1", (nm,)).fetchone()
                    if row:
                        pid_of[nm] = int(row[0])
                    else:
                        cur2 = self._conn.execute(
                            "INSERT INTO people(name, kind, n_faces, created) "
                            "VALUES(?,?,?,?)", (nm, "person", 0, time.time()))
                        pid_of[nm] = cur2.lastrowid
                for fid, nm in snap.items():
                    self._conn.execute(
                        "UPDATE faces SET person_id=? WHERE id=?", (pid_of[nm], fid))
                    restored += 1
                # 修正每组的脸数、封面；清掉空组
                self._conn.execute(
                    "UPDATE people SET n_faces=(SELECT COUNT(*) FROM faces "
                    "WHERE person_id = people.id)")
                self._conn.execute(
                    "UPDATE people SET cover_face=(SELECT id FROM faces "
                    "WHERE person_id = people.id "
                    "ORDER BY (prob*(x2-x1)*(y2-y1)) DESC LIMIT 1)")
                self._conn.execute("DELETE FROM people WHERE n_faces = 0")
                made = self._conn.execute(
                    "SELECT COUNT(*) FROM people").fetchone()[0]
        return {"groups": made, "faces": len(ids), "dim": dim,
                "restored": restored}

    def files_with_people(self, person_ids) -> set[int]:
        ids = [int(x) for x in person_ids if str(x).strip()]
        if not ids:
            return set()
        ph = ",".join("?" * len(ids))
        return {int(r["file_id"]) for r in self._query(
            f"SELECT DISTINCT file_id FROM faces WHERE person_id IN ({ph})", ids)}

    def file_faces(self, file_id: int) -> list[dict]:
        return [{"id": int(r["id"]), "person_id": r["person_id"],
                 "box": [r["x1"], r["y1"], r["x2"], r["y2"]], "prob": r["prob"]}
                for r in self._query(
                    "SELECT * FROM faces WHERE file_id=? ORDER BY prob DESC", (file_id,))]

    def face_stats(self) -> dict:
        row = self._query("SELECT COUNT(*) n, COUNT(DISTINCT file_id) f FROM faces")[0]
        ppl = self._query("SELECT COUNT(*) n FROM people WHERE name<>''")[0]
        return {"faces": int(row["n"]), "files": int(row["f"]),
                "named_people": int(ppl["n"])}

    # ---------------- 只补提人脸（不重新编码）----------------
    def _face_marker_key(self, folder_id: int | None) -> str:
        # 每个文件夹一个独立水位，避免扫了 A 之后 B 被跳过
        return f"face_backfill_id:{int(folder_id)}" if folder_id else "face_backfill_id"

    def faces_pending(self, limit: int = 20, folder_id: int | None = None) -> list[dict]:
        """还没做过人脸提取的图片。

        folder_id 给了就只处理那个文件夹，否则处理全部（按文件 id 顺序）。
        用 meta 里的水位标记推进，已处理过的不会重复扫。
        """
        marker = int(self.get_meta(self._face_marker_key(folder_id)) or 0)
        if folder_id:
            return [dict(r) for r in self._query(
                "SELECT id, path, name FROM files WHERE kind='image' AND status='ok' "
                "AND folder_id=? AND id > ? ORDER BY id LIMIT ?",
                (int(folder_id), marker, limit))]
        return [dict(r) for r in self._query(
            "SELECT id, path, name FROM files WHERE kind='image' AND status='ok' "
            "AND id > ? ORDER BY id LIMIT ?", (marker, limit))]

    def set_face_marker(self, file_id: int, folder_id: int | None = None) -> None:
        self.set_meta(self._face_marker_key(folder_id), str(int(file_id)))

    def faces_remaining(self, folder_id: int | None = None) -> int:
        marker = int(self.get_meta(self._face_marker_key(folder_id)) or 0)
        if folder_id:
            row = self._query(
                "SELECT COUNT(*) n FROM files WHERE kind='image' AND status='ok' "
                "AND folder_id=? AND id > ?", (int(folder_id), marker))
        else:
            row = self._query(
                "SELECT COUNT(*) n FROM files WHERE kind='image' AND status='ok' "
                "AND id > ?", (marker,))
        return int(row[0]["n"]) if row else 0

    def reset_face_marker(self, folder_id: int | None = None) -> None:
        self.set_meta(self._face_marker_key(folder_id), "0")

    # ================= 混合搜索 =================
    def hybrid_search(self, query_vec=None, *, top_k: int = 30,
                      folder_ids=None, exts=None, literal: str = "",
                      person_ids=None, person_mode: str = "filter",
                      face_vec=None, face_threshold: float = 0.45,
                      weights: dict | None = None) -> list[dict]:
        """多路召回 + 融合排序。

        召回通道：
            semantic  内容 / 文件名向量（现有能力）
            literal   文件名、路径包含匹配
            fts       片段正文的关键词子串（FTS5 逐字索引，专有名词靠它 ✓ 多人 ⑤）
            face      人脸特征比对（以图搜人）
            person    人物筛选
        融合：RRF（倒数排名融合）—— 各路分数量纲不同，比直接加权稳。
        """
        w = {"semantic": 1.0, "literal": 0.8, "fts": 1.1, "face": 1.2,
             "person": 0.7, "name_hit": 0.6}
        w.update(weights or {})

        ranks: dict[int, dict] = {}

        def add(fid: int, channel: str, rank: int, score: float = 0.0):
            item = ranks.setdefault(int(fid), {"channels": {}})
            item["channels"].setdefault(
                channel, {"rank": rank, "score": round(float(score), 4)})

        base: list[dict] = []
        if query_vec is not None:
            base = self.search(query_vec, top_k=max(top_k * 4, 80),
                               folder_ids=folder_ids, exts=exts, literal=literal)
            for i, r in enumerate(base):
                add(r["id"], "semantic", i, r.get("score", 0.0))

        if literal.strip():
            for i, r in enumerate(self.literal_search(literal, max(top_k * 3, 60))):
                add(r["id"], "literal", i, 0.0)

        # 关键词通道（FTS5）：中英文的人名/编号/术语，向量不敏感、这里精确命中 ✓
        fts_hits: list[dict] = []
        if literal.strip():
            fts_hits = self.fts_search(literal, top_k=max(top_k * 3, 60),
                                       folder_ids=folder_ids, exts=None)
            for i, r in enumerate(fts_hits):
                add(r["id"], "fts", i, r.get("score", 0.0))

        if face_vec is not None:
            for i, h in enumerate(self.search_faces(
                    face_vec, top_k=max(top_k * 4, 120), threshold=face_threshold)):
                add(h["file_id"], "face", i, h["sim"])

        allowed: set[int] | None = None
        if person_ids:
            allowed = self.files_with_people(person_ids)
            if person_mode == "filter":
                for fid in list(ranks):
                    if fid not in allowed:
                        del ranks[fid]
                for fid in allowed:
                    ranks.setdefault(int(fid), {"channels": {}})
                    ranks[int(fid)]["channels"].setdefault(
                        "person", {"rank": 0, "score": 1.0})
            else:
                for fid in allowed:
                    add(fid, "person", 0, 1.0)

        if not ranks:
            return []

        K = 60.0
        scores = {fid: sum(w.get(ch, 0.5) / (K + info["rank"] + 1)
                           for ch, info in item["channels"].items())
                  for fid, item in ranks.items()}

        ids = list(ranks)
        ph = ",".join("?" * len(ids))
        meta = {row["id"]: row for row in self._query(
            f"SELECT * FROM files WHERE id IN ({ph})", ids)}

        needle = literal.strip().lower()
        snips = {r["id"]: r.get("snippet", "") for r in base}
        # FTS 命中的片段更贴合关键词（向量那边的片段是按语义挑的）→ 优先用它 ✓
        for r in fts_hits:
            if r.get("snippet"):
                snips[int(r["id"])] = r["snippet"]
        out: list[dict] = []
        for fid in ids:
            row = meta.get(fid)
            if row is None:
                continue
            if folder_ids and row["folder_id"] not in folder_ids:
                continue
            if exts and (row["ext"] or "") not in exts:
                continue
            ch = ranks[fid]["channels"]
            if allowed is not None and person_mode == "filter" and fid not in allowed:
                continue
            if needle and needle in row["name"].lower():
                scores[fid] += w["name_hit"] / K
                ch["name_hit"] = {"rank": 0, "score": 1.0}
            out.append({
                "id": fid, "folder_id": row["folder_id"], "path": row["path"],
                "name": row["name"], "dir": row["dir"], "ext": row["ext"],
                "kind": row["kind"], "size": row["size"], "mtime": row["mtime"],
                "status": row["status"], "note": row["note"],
                "n_chunks": row["n_chunks"],
                "score": round(scores[fid] * 100, 3),
                "channels": ch,
                "snippet": (snips.get(fid) or "")[:600],
            })
        out.sort(key=lambda r: -r["score"])
        return out[:top_k]

    def literal_search(self, needle: str, top_k: int = 30) -> list[dict[str, Any]]:
        """纯文件名/路径包含匹配，作为向量检索的补充。"""
        like = f"%{needle}%"
        rows = self._query(
            "SELECT * FROM files WHERE name LIKE ? OR path LIKE ? "
            "ORDER BY (name LIKE ?) DESC, mtime DESC LIMIT ?",
            (like, like, f"{needle}%", top_k),
        )
        return [{
            "id": row["id"], "folder_id": row["folder_id"], "path": row["path"],
            "name": row["name"], "dir": row["dir"],
            "ext": row["ext"], "kind": row["kind"], "size": row["size"], "mtime": row["mtime"],
            "status": row["status"], "note": row["note"], "n_chunks": row["n_chunks"],
            "score": 0.0, "snippet": "",
        } for row in rows]

    # ---------------- 关键词检索（FTS5，多人 ⑤）----------------
    def fts_search(self, needle: str, top_k: int = 60,
                   folder_ids: list[int] | None = None,
                   exclude_kinds: tuple | None = None) -> list[dict[str, Any]]:
        """在**片段正文**里做子串级关键词检索（不是只比文件名 ✓）。

        为什么需要它：向量检索对专有名词不敏感（人名、编号、书名、命令），
        而这些恰恰是知识库里最该精确命中的东西。老代码是用
        `chunks.text LIKE '%kw%'` 全表扫（每次 65~90ms，还要按关键词重复扫 ✗），
        现在换成 FTS5 逐字索引 + 短语查询：**<1ms**，而且是子串语义 ✓。

        返回按文件聚合（同一文件的多个命中取最好的那个片段），带真实片段 ✓
        """
        from . import ftsindex as _fts                            # noqa: PLC0415

        if not self.fts_state.get("ok"):
            return []                                             # 没建成 → 调用方自己走老的 LIKE ✓
        hits = _fts.search(self._conn, needle, limit=max(int(top_k) * 3, 60),
                           folder_ids=folder_ids, exclude_kinds=exclude_kinds)
        if not hits:
            return []
        best: dict[int, dict] = {}
        for h in hits:
            fid = int(h["file_id"])
            cur = best.get(fid)
            if cur is None or h["score"] > cur["score"]:
                best[fid] = h
        ids = list(best.keys())
        ph = ",".join("?" * len(ids))
        meta = {int(r["id"]): r for r in self._query(
            f"SELECT * FROM files WHERE id IN ({ph})", ids)}
        out: list[dict[str, Any]] = []
        for fid, h in best.items():
            row = meta.get(fid)
            if row is None:
                continue
            out.append({
                "id": fid, "folder_id": row["folder_id"], "path": row["path"],
                "name": row["name"], "dir": row["dir"], "ext": row["ext"],
                "kind": row["kind"], "size": row["size"], "mtime": row["mtime"],
                "status": row["status"], "note": row["note"],
                "n_chunks": row["n_chunks"],
                "score": float(h["score"]), "snippet": h["snippet"],
                "chunk_id": int(h["chunk_id"]),
            })
        out.sort(key=lambda r: -r["score"])
        return out[:top_k]

    def fts_chunks(self, needle: str, limit: int = 60,
                   folder_ids: list[int] | None = None,
                   exclude_kinds: tuple | None = None) -> list[dict[str, Any]]:
        """片段级的关键词命中（问答/知识库检索预览要按片段给上下文 ✓）。"""
        from . import ftsindex as _fts                            # noqa: PLC0415

        if not self.fts_state.get("ok"):
            return []
        hits = _fts.search(self._conn, needle, limit=limit, folder_ids=folder_ids,
                           exclude_kinds=exclude_kinds)
        ids = list({h["file_id"] for h in hits})
        meta = {}
        if ids:
            ph = ",".join("?" * len(ids))
            meta = {int(r["id"]): r for r in self._query(
                f"SELECT id, name, path, dir, folder_id FROM files WHERE id IN ({ph})", ids)}
        out = []
        for h in hits:
            row = meta.get(int(h["file_id"]))
            if row is None:
                continue
            out.append({**h, "name": row["name"], "path": row["path"],
                        "dir": row["dir"]})
        return out
