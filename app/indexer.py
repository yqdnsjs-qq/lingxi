"""文件夹索引器：扫描 → 抽取文本 → 向量化 → 存库。

全部在后台线程里跑，进度通过 /api/index/status 查询；
已索引的文件按「大小 + 修改时间」跳过，删除的文件会从索引里清掉。
"""
from __future__ import annotations

import bisect
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import extract
from .config import Config
from .folderconf import FolderConfig, clean_subcats, subcat_override
from .model import EmbeddingEngine
from .store import Store

log = logging.getLogger("eg2.index")

# 把底层库的长篇报错翻译成人看得懂的一句话
_ERROR_HINTS = (
    ("Unsupported image file", "图片格式不支持（只支持 jpeg/png/webp/gif，"
                               "HEIC/AVIF 等需要先转换）"),
    ("Permission denied", "没有读取权限（网络共享可能未连接或凭据失效）"),
    ("PermissionError", "没有读取权限（网络共享可能未连接或凭据失效）"),
    ("No such file", "文件已被移动或删除"),
    ("cannot identify image file", "无法识别的图片格式（文件可能损坏或其实是别的格式）"),
    ("Errno 13", "没有读取权限"),
)


def _readable_error(exc: Exception) -> str:
    text = str(exc)
    for needle, friendly in _ERROR_HINTS:
        if needle.lower() in text.lower():
            return friendly
    return text.strip().splitlines()[0][:160]


class Indexer:
    def __init__(self, cfg: Config, store: Store, engine: EmbeddingEngine):
        self._face_note = ""
        self.cfg = cfg
        self.store = store
        self.engine = engine
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._warn_text = ""
        # 时限（2026-10-19）：绝对时间戳，到点优雅暂停；None = 不限
        self._deadline: float | None = None
        self._dirs_skipped = 0            # 快速扫描跳过了多少个目录（⑥）
        self._lock = threading.Lock()
        self._status: dict[str, Any] = self._idle_status()

    # ------------------------------------------------------------ 状态
    @staticmethod
    def _idle_status() -> dict:
        return {
            "running": False, "phase": "空闲", "folder": "", "current": "",
            "total": 0, "done": 0, "indexed": 0, "skipped": 0, "removed": 0,
            "errors": 0, "chunks": 0, "started_at": None, "finished_at": None,
            "message": "",
            "faces": 0, "face_note": "",
            "folder_total": 0, "folder_index": 0, "folder_files": 0, "folder_done": 0,
            "dirs_skipped": 0,
            "deadline": 0,
        }

    def status(self) -> dict:
        with self._lock:
            data = dict(self._status)
        data["face_note"] = self._face_note
        data["dirs_skipped"] = self._dirs_skipped
        if data["running"] and data["started_at"]:
            data["elapsed"] = round(time.time() - data["started_at"], 1)
        else:
            data["elapsed"] = 0
        # 还剩多少时间到时限（界面可以显示"还剩 3 小时 20 分"）
        dl = float(data.get("deadline") or 0)
        data["remaining"] = (max(0.0, dl - time.time()) if (data["running"] and dl)
                             else 0.0)
        return data

    def _update(self, **kwargs) -> None:
        with self._lock:
            self._status.update(kwargs)

    # ------------------------------------------------------------ 控制
    def cancel(self) -> None:
        self._cancel.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, folder_ids: list[int] | None = None, force: bool = False,
              deadline: float | None = None) -> bool:
        """启动索引。

        `deadline`（2026-10-19 项目主："全显存方式跑…要有时限控制"）：
          绝对时间戳；到点**优雅暂停**（做完当前文件就停，进度保留 ✓），
          之后点「开始」能接着跑 ✓。None = 不限时。
        """
        if self.is_running():
            return False
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(folder_ids, force, deadline),
            name="eg2-indexer", daemon=True)
        self._thread.start()
        return True

    # ------------------------------------------------------------ 主流程
    def _run(self, folder_ids: list[int] | None, force: bool,
             deadline: float | None = None) -> None:
        folders = self.store.list_folders()
        if folder_ids:
            folders = [f for f in folders if f["id"] in folder_ids]
        self._warn_text = ""
        self._deadline = deadline
        self._update(**self._idle_status())
        self._update(running=True, started_at=time.time(), finished_at=None,
                     message="正在扫描目录…",
                     deadline=deadline or 0)
        timed_out = False
        try:
            total_folders = len(folders)
            for index, folder in enumerate(folders, start=1):
                if self._cancel.is_set():
                    break
                if deadline and time.time() >= deadline:
                    timed_out = True
                    log.info("到达时限，优雅暂停（进度已保留，下次接着跑）")
                    break
                self._update(folder_total=total_folders, folder_index=index,
                             folder_files=0, folder_done=0)
                self._scan_folder(folder, force)
        except Exception as exc:  # noqa: BLE001
            log.exception("索引失败")
            self._update(message=f"出错：{exc}")
        finally:
            if self._cancel.is_set():
                final = "已取消"
            elif timed_out:
                final = "到时限已暂停（进度保留，点「开始」可接着跑）"
            elif self._warn_text:
                final = f"索引完成（有提醒：{self._warn_text}）"
            else:
                final = "索引完成"
            self._update(running=False, finished_at=time.time(),
                         phase="空闲", current="", message=final)
            self._cancel.clear()

    def _walk(self, root: Path, fcfg=None,
              dir_cache: dict[str, tuple[float, int]] | None = None,
              known_under: "callable | None" = None):
        """遍历目录，产出 `(路径, stat)`。

        多人 ⑥（索引加速）：这几点是拿真实 NAS 计时换来的 ✓
          1. **不再对每个文件单独 `path.stat()`** —— `os.scandir` 的 DirEntry
             在枚举时就已经带回 stat 了，再 stat 一次只是白等一个 SMB 往返 ✗
             （实测 4 个 NAS 文件夹：249.5s → 5.1s，约 **49x**）
          2. **目录 mtime 缓存**（可选，`fast_scan`）：目录没变过就不下去列目录 ✓
             ⚠ 只对**叶子目录**（缓存里记着"没有子目录"且 mtime 没变）生效 ——
             这一条是被测试逼出来的：新建在**深层子目录**里的文件只会改那个子目录的
             mtime，**不会**改父目录的 mtime ✗，所以"父目录没变就整棵跳过"会把新文件
             漏掉 ✗（第一版就是这么错的）。叶子目录没有子目录，所以它 mtime 没变
             ⇒ 里面不可能有新增/改名/删除 ✓（只有"就地改文件内容"发现不了，
             这是这个开关的已知取舍，写在配置项的 help 里 ✓）
          3. 跳过的目录要把**索引里已有的文件**补进 seen（`known_under` 回调）✓
             否则会被当成"文件都消失了"而误删 ✗
        """
        source = fcfg or self.cfg
        ignore = source.ignore_dirs
        skip_ext = source.skip_ext
        use_cache = dir_cache is not None and bool(getattr(source, "fast_scan", False))
        stack = [root]
        while stack:
            current = stack.pop()
            if use_cache:
                # 关键顺序：**先**看缓存能不能整目录跳过（跳过的目录一次都不列 ✓），
                # 只有不能跳时才去列目录。反过来写（先列后判）等于没优化 ✗
                prev = dir_cache.get(str(current))
                if prev is not None and int(prev[1]) == 0:
                    try:
                        dir_mtime = os.stat(current).st_mtime
                    except OSError:
                        continue
                    if abs(prev[0] - dir_mtime) < 1e-6:
                        # 缓存说它是叶子、现在确实没子目录、mtime 又没变 → 安全跳过 ✓
                        if known_under is not None:
                            known_under(current)
                        self._dirs_skipped += 1
                        continue
            try:
                entries = list(os.scandir(current))
            except (PermissionError, OSError):
                continue
            if use_cache:
                try:
                    dir_mtime = os.stat(current).st_mtime
                except OSError:
                    continue
                n_sub = 0
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False) and not e.name.startswith("$") \
                                and e.name.lower() not in ignore:
                            n_sub += 1
                    except OSError:
                        continue
                dir_cache[str(current)] = (dir_mtime, n_sub)
            for entry in entries:
                if self._cancel.is_set():
                    return
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name
                        if name.startswith("$") or name.lower() in ignore:
                            continue
                        # 文件夹自己的排除规则
                        if fcfg is not None and fcfg.excluded(
                                Path(entry.path), root, True):
                            continue
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        if Path(entry.name).suffix.lower() in skip_ext:
                            continue
                        if fcfg is not None and fcfg.excluded(
                                Path(entry.path), root, False):
                            continue
                        # DirEntry.stat()：枚举时拿到的，不再多一次往返 ✓
                        yield Path(entry.path), entry.stat()
                except OSError:
                    continue

    # ------------------------------------------------------------ 排除预览
    def preview_exclude(self, root: Path, fcfg=None, limit: int = 60000,
                        max_seconds: float = 20.0) -> dict:
        """只看看排除规则会拦掉什么，不写任何东西。

        用界面上**当前**的规则（可能还没保存），这样能先验证再保存。

        ⚠ 2026-10-18：加了**时间上限** ✓ —— 项目主对 NAS 文件夹点"预览排除"，
        遍历整台 NAS 要好几分钟，界面那边等到超时/关掉，服务就被拖进了退出流程 ✗。
        现在最多跑 max_seconds 秒，超了就返回**已经扫到的部分**并标 `truncated` ✓
        （结果依然有用 ✓ 只是不完整）。
        """
        source = fcfg or self.cfg
        ignore = {d.lower() for d in source.ignore_dirs}
        skip_ext = {e.lower() for e in source.skip_ext}
        deadline = time.monotonic() + max(1.0, float(max_seconds))

        total = kept = 0
        by_ext: dict[str, int] = {}
        by_dir: dict[str, int] = {}
        samples: list[dict] = []
        truncated = False
        time_up = False

        stack = [root]
        while stack:
            if self._cancel.is_set():
                break
            if time.monotonic() > deadline:
                truncated = True
                time_up = True
                break
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except (PermissionError, OSError):
                continue
            for entry in entries:
                total += 1
                if total > limit:
                    truncated = True
                    break
                path = Path(entry.path)
                try:
                    try:
                        rel = path.relative_to(root).as_posix()
                    except ValueError:
                        rel = entry.name

                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name
                        reason = None
                        if name.startswith("$"):
                            reason = "系统目录（$ 开头）"
                        elif name.lower() in ignore:
                            reason = "全局忽略的目录名"
                        elif fcfg is not None and fcfg.excluded(path, root, True):
                            reason = "你设的排除规则"
                        if reason:
                            by_dir[rel] = by_dir.get(rel, 0) + 1
                            if len(samples) < 40:
                                samples.append({"path": rel + "/", "reason": reason,
                                                "is_dir": True})
                            continue
                        stack.append(path)
                        continue

                    if not entry.is_file(follow_symlinks=False):
                        continue
                    ext = path.suffix.lower()
                    reason = None
                    if ext and ext in skip_ext:
                        reason = f"全局跳过的扩展名 {ext}"
                    elif fcfg is not None and fcfg.excluded(path, root, False):
                        reason = "你设的排除规则"
                    if reason:
                        key = ext or "(无扩展名)"
                        by_ext[key] = by_ext.get(key, 0) + 1
                        if len(samples) < 40:
                            samples.append({"path": rel, "reason": reason,
                                            "is_dir": False})
                        continue
                    kept += 1
                except OSError:
                    continue
            if truncated:
                break

        excluded = total - kept
        top_ext = sorted(by_ext.items(), key=lambda kv: -kv[1])[:10]
        return {
            "root": str(root),
            "scanned": total,
            "kept": kept,
            "excluded": excluded,
            "truncated": truncated,
            # 时间不够了（而不是"扫满了"）→ 前端可以提示"只预览了一部分"
            "time_up": time_up,
            "max_seconds": max_seconds,
            "excluded_dirs": len(by_dir),
            "top_ext": [{"ext": k, "count": v} for k, v in top_ext],
            "samples": samples[:40],
            "rules": list(fcfg.exclude) if fcfg is not None else [],
        }

    def _scan_folder(self, folder: dict, force: bool) -> None:
        # ★ 这台机器该去哪个目录找（2026-10-20 项目主："服务器接上"）
        #   本机（建库机）→ local_alias 优先（"本机文件夹也可自定义同步目录"）
        #   服务器        → server_path 优先（库里 path 是 Windows 视角，Linux 打不开 ✗）
        #   ⚠ 数据库里 files.path 仍以 folders.path 为前缀（权威键不变）✓
        # ⚠ 2026-10-20 修：这里原来写的是 `store.folder_paths(folder_id)` ✗ ——
        #   两个名字在这个方法里**都不存在**（`store` 只在 __init__ 里存成 self.store，
        #   `folder_id` 还没解出来）→ 每次扫描第一行就 NameError:
        #   `name 'store' is not defined` → 「全力执行」的索引阶段整体失败 ✗
        #   （打标不受影响，所以从界面上看像是"索引没动手、只有打标在跑"）
        folder_id = int(folder["id"])
        from . import serverpath as _sp
        paths = self.store.folder_paths(folder_id)
        local_root = _sp.pick_root(paths["path"], paths["local"], paths["server"])
        if local_root != str(folder["path"]):
            log.info("文件夹 #%s 用的是%s路径：%s（库里的权威键是 %s）",
                     folder_id, "服务器" if _sp.is_server() else "本机",
                     local_root, folder["path"])
        root = Path(local_root)
        try:
            accessible = os.path.isdir(str(root))
        except OSError:
            accessible = False
        if not accessible:
            self._update(message=f"文件夹不存在或无法访问：{root}")
            return

        self._update(folder=str(root), phase="扫描目录", current="", message=f"正在扫描 {root}")
        fcfg = FolderConfig(self.cfg, folder.get("config"), self.engine)
        # ---- 子目录归类（2026-10-17 项目主确认：只做一层）----
        # 同一个文件夹内部的**第一层**子目录可以各自用不同口径 ✓
        # （比如「照片」当照片库、「素材」当影音库，其余跟随本层）。
        # 不给每个子目录建独立文件夹条目 ✗（那样太麻烦），只在这里预建好几份配置 ✓。
        fcfg_raw = folder.get("config") or {}
        sub_cfgs: dict[str, FolderConfig] = {}
        for _name, _val in clean_subcats(fcfg_raw.get("subcats")).items():
            # 值可能是预设归类（"kb"）也可能是自定义勾选（{"index_text": true, ...}）✓
            _cat, _custom = (("custom", _val) if isinstance(_val, dict)
                             else (str(_val), {}))
            sub_cfgs[_name] = FolderConfig(
                self.cfg, subcat_override(fcfg_raw, _cat, _custom), self.engine)
        root_path = Path(root)

        def sub_of(p: Path) -> str:
            """这个文件属于哪个归了类的子目录（返回**子目录名**；没命中返回空串）✓

            ⚠ 2026-10-17 验收抓到的坑：这里原来返回的是**类别 id**（"kb"）✗，
            而 `sub_cfgs` 是按**子目录名**（"文章"）索引的 → 查不到 → 静默退回本层口径
            → 表现成"子目录归类完全没生效"✗。名字对名字，才接得上 ✓。
            """
            if not sub_cfgs:
                return ""
            try:
                rel = p.relative_to(root_path)
            except ValueError:
                return ""
            parts = [x for x in str(rel).replace("\\", "/").split("/") if x]
            if len(parts) < 2:            # 根目录下的文件 → 本层口径 ✓
                return ""
            return parts[0] if parts[0] in sub_cfgs else ""

        known = self.store.get_files_map(folder_id)
        work: list[tuple[Path, os.stat_result, str]] = []
        seen: set[str] = set()

        # ---- 多人 ⑥：目录 mtime 缓存（只对开了 fast_scan 的文件夹生效 ✓）----
        # ⚠ force（重建索引）时**绝不能用缓存** —— 用户的期望是"全量重扫一遍"，
        # 用缓存跳过叶子目录会让叶子里的内容改动仍然漏掉 ✗（测试抓出来的）
        dir_cache: dict[str, tuple[float, int]] = {}
        known_sorted: list[str] = []
        if getattr(fcfg, "fast_scan", False) and not force:
            dir_cache = self.store.dir_mtimes(folder_id)
            known_sorted = sorted(known)
            self._update(message=f"正在扫描 {root}（快速模式：跳过没变过的叶子目录）")

        def mark_known_under(directory: Path) -> None:
            """被跳过的目录：把它底下**索引里已有的文件**标成 seen ✓（防误删）"""
            prefix = str(directory) + os.sep
            i = bisect.bisect_left(known_sorted, prefix)
            while i < len(known_sorted) and known_sorted[i].startswith(prefix):
                seen.add(known_sorted[i])
                i += 1

        for path, stat in self._walk(root, fcfg, dir_cache=dir_cache,
                                     known_under=mark_known_under):
            if self._cancel.is_set():
                return
            key = str(path)
            seen.add(key)
            self._update(total=len(seen), folder_files=len(seen))
            old = known.get(key)
            if (not force and old is not None and old["status"] == "ok"
                    and int(old["size"]) == stat.st_size
                    and abs(float(old["mtime"]) - stat.st_mtime) < 1e-6
                    and not self._ocr_wants_resume(old)):
                self._update(skipped=self.status()["skipped"] + 1)
                continue
            work.append((path, stat, sub_of(path)))

        if dir_cache:
            try:
                self.store.save_dir_mtimes(folder_id, dir_cache)
            except Exception as exc:                              # noqa: BLE001
                log.warning("目录 mtime 缓存写入失败（不影响本次索引）：%s", exc)

        # 安全阀：网络共享中途掉线、权限异常时，可能一个文件都枚举不到。
        # 这时候绝不能把「没扫到」当成「文件都删了」，否则一次抖动就清空整个索引。
        known_count = len(known)
        found = len(seen)
        warning = ""
        suspicious = known_count > 0 and (
            found == 0 or (known_count >= 20 and found < known_count * 0.2))
        if suspicious and not force:
            removed = 0
            warning = (f"{root} 只读到 {found} 个文件（索引里原有 {known_count} 个），"
                       f"已跳过删除以免误清索引，请确认网络共享连接正常")
            self._warn_text = warning
            log.warning("扫描 %s 只枚举到 %d 个文件，而索引里原有 %d 个；"
                        "判定为共享不可用或权限异常，跳过删除", root, found, known_count)
        else:
            removed = self.store.delete_paths([p for p in known if p not in seen])
        self._update(removed=removed, total=found)

        total_bytes = sum(s.st_size for _, s, _sub in work)
        self._update(phase="建立索引", message=f"待索引 {len(work)} 个文件"
                     f"（{total_bytes / 1e6:.1f} MB）· {fcfg.summary()['label']}")
        self._index_files(folder_id, work, fcfg, sub_cfgs)
        self.store.set_folder_scan(folder_id, "idle")
        if warning:
            self._update(message=warning)

    # ------------------------------------------------------------ 人脸
    def _extract_faces(self, file_id: int, path: Path) -> int:
        """提取人脸并入库。返回检出数量；-1 表示人脸功能不可用。

        刻意把所有异常都吞掉：人脸是可选功能，出问题绝不能影响
        文本/语义索引这条主流程。
        """
        if not self.cfg.face_enabled:
            return -1
        try:
            from . import faces as face_mod
            ok, why = face_mod.available()
            if not ok:
                self._face_note = why
                return -1
            from PIL import Image
            with Image.open(path) as im:
                found = face_mod.engine().extract(
                    im, min_prob=self.cfg.face_min_prob,
                    min_face=self.cfg.face_min_size)
            self.store.clear_file_faces(file_id)
            if found:
                self.store.add_faces(
                    file_id, [(f.box, f.prob, f.vector) for f in found])
            return len(found)
        except Exception as exc:                      # noqa: BLE001
            self._face_note = f"{type(exc).__name__}: {exc}"[:120]
            return -1

    # ------------------------------------------------------------ OCR 续做判定
    def _ocr_wants_resume(self, old) -> bool:
        """这个文件是不是"OCR 还没做完、应该再扫一次"✓

        项目主的追问（2026-10-19）："调高页码数后，原已 OCR 的页面不会重新 OCR 吧"
        → 两层保证：
          ① ocr_pdf 支持 start_page（从下一页接着做，前面的不重做）✓
          ② **这里**让"上限调高了"的文件**不再被跳过**，否则根本没机会续做 ✓
        判据：上次真的做过 OCR（was > 0）、上次的上限比现在小（was < now）、
              而且确实已经有进度（done > 0）→ 该续做 ✓
        """
        try:
            if old is None:
                return False
            if str(old["kind"] or "") != "pdf":
                return False
            done = int(old["ocr_pages"] or 0)
            was = int(old["ocr_limit"] or 0)
            now = int(getattr(self.cfg, "ocr_max_pages", 100) or 100)
            # ★ 单份设过页码范围（项目主 2026-10-19："以个别设置为准"）：
            #   只要还没做到那个范围的末尾，就该重扫 ✓
            try:
                one_from = int(old["ocr_from"] or 0)
                one_to = int(old["ocr_to"] or 0)
            except (IndexError, KeyError):
                one_from = one_to = 0
            if one_from > 0 or one_to > 0:
                if one_to <= 0:
                    return True            # 到末页 → 除非做过，否则要扫 ✓
                return done < one_to
            return was > 0 and was < now and done > 0
        except Exception:                                        # noqa: BLE001
            return False

    # ------------------------------------------------------------ 扫描版 OCR
    def _ocr_pdf_text(self, path: Path, file_id: int) -> tuple[str, str]:
        """对"提不出文字"的 PDF 做 OCR，返回 (文字, 给用户看的备注) ✓

        项目主 2026-10-19：
          "放文件进去能自动判断自动 OCR 进知识库" ✓
          "页面限制太死板…要有个提示和调节选项，超过页码限制能提醒我，也能手动修改" ✓

        要点：
          · 页数受 `ocr_max_pages` 约束（默认 100）；超了**只做前 N 页** ✓
          · 备注里写清：共多少页、做了多少、还剩多少、怎么调大 ✓
            → 这句会存进文件的 note，界面上点「配置」或看备注就能看到 ✓
          · 引擎由 app/ocr.py 自动挑（MinerU 优先，不可用退回 PP-OCRv6）✓
          · 走 gpu_slot 排队（不和别的事抢显存）✓ —— 这在 ocr_pdf 里已经做了 ✓
        """
        from . import ocr as _ocr
        try:
            self._update(message=f"扫描版 OCR：{path.name}")
            limit = int(getattr(self.cfg, "ocr_max_pages", 100) or 100)
            # ★ 接着做：上次已 OCR 到第 N 页 → 这次从 N+1 开始（不重做）✓
            #   项目主："调高页码数后，原已 OCR 的页面不会重新 OCR 吧"
            done_before, _old_limit = self.store.ocr_progress(file_id)
            start_page = (done_before + 1) if done_before > 0 else 1
            # ★ 页码范围：**单份 PDF 的设置优先级最高**（项目主 2026-10-19：
            #   "个别取页码范围，以个别设置为准"）✓
            #   单份设过（ocr_from > 0）→ 完全按它来，**不看全局** ✓
            #   没设过 → 用全局的 ocr_page_from / ocr_page_to ✓
            one_from, one_to = self.store.ocr_pages_of(file_id)
            if one_from > 0 or one_to > 0:
                want_from, want_to = (one_from or 1), max(0, one_to)
                start_page = max(1, want_from)      # 单份设的起始页说了算 ✓
                use_limit = 0                       # 也不受全局页数上限约束 ✓
                log.info("扫描版 OCR 用**单份设置**的页码：%s 第 %s~%s 页",
                         path.name, want_from, want_to or "末")
            else:
                want_from = int(getattr(self.cfg, "ocr_page_from", 1) or 1)
                want_to = int(getattr(self.cfg, "ocr_page_to", 0) or 0)
                start_page = max(start_page, want_from)
                use_limit = limit
                if done_before:
                    log.info("扫描版 OCR 续做：%s 已做完前 %d 页 → 从第 %d 页开始",
                             path.name, done_before, start_page)
            r = _ocr.ocr_pdf(path, engine="", max_pages=use_limit,
                             start_page=start_page, end_page=want_to)
            # 记下这次做到第几页（下次接着做）✓
            if r.get("page_to"):
                self.store.set_ocr_progress(file_id, int(r["page_to"]), limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描版 OCR 失败 %s：%s", path.name, exc)
            return "", f"扫描版 OCR 失败：{type(exc).__name__}: {exc}"
        if not r.get("ok"):
            return "", (r.get("notice") or "扫描版 OCR：没识别出文字")
        bits = []
        if r.get("notice"):
            bits.append(str(r["notice"]))
        bits.append(f"扫描版 OCR：本次处理第 {r.get('page_from')}~{r.get('page_to')} 页"
                    f"（共 {r.get('total_pages')} 页；引擎 {r.get('engine')}，"
                    f"{r.get('elapsed')} 秒）")
        return str(r.get("text") or ""), "；".join(x for x in bits if x)

    # ------------------------------------------------------------ 插图登记
    def _enqueue_pdf_images(self, file_id: int, folder_id: int, path: Path,
                            fcfg) -> int:
        """把 PDF 里**值得打标的插图**登记进队列（只登记，不调用模型 ✓）。

        项目主 2026-10-19 选定的用法：
          · 档位「只登记」→ 白天索引时只排队（秒过、不占显存、不花 API），
            夜里点「全力执行」才真正打标 ✓
          · 档位「自动」→ 这里同样只登记；执行由执行器负责（下一步接入）
        筛选规则在 app/pdfimages.py：丢掉图标/装饰/重复，每个文件有上限 ✓
        """
        try:
            from . import pdfimages
            # ★ 0 = 全部标（2026-10-19 项目主："默认 PDF 插图自动全部打标"）✓
            limit = int(getattr(self.cfg, "ocr_max_images", 0) or 0)
            items = pdfimages.list_images(path, limit=(limit if limit > 0 else 9999))
            if not items:
                return 0
            added = self.store.caption_enqueue(file_id, folder_id, items)
            if added:
                stats = self.store.caption_queue_stats()
                log.info("登记插图待打标：%s 新增 %d 张（档位 %s，队列共 %d 张待办）",
                         path.name, added, fcfg.caption_mode, stats.get("pending", 0))
                self._update(caption_pending=stats.get("pending", 0))
            return added
        except Exception as exc:  # noqa: BLE001
            # 登记失败绝不能影响索引主流程 ✓（它只是"顺手排队"）
            log.debug("登记插图失败（不影响索引）：%s", exc)
            return 0

    # ------------------------------------------------------------ 索引文件
    def _index_files(self, folder_id: int,
                     work: list[tuple[Path, os.stat_result, str]],
                     fcfg: FolderConfig | None = None,
                     sub_cfgs: dict | None = None) -> None:
        cfg = fcfg or FolderConfig(self.cfg, None, self.engine)
        batch = max(1, cfg.search_batch_size)
        max_bytes = cfg.max_file_mb * 1024 * 1024
        text_pending: list[tuple[int, str, str]] = []        # (file_id, 文本, 类型)
        media_pending: list[tuple[int, Path, Any, str]] = []  # (file_id, 路径, 载荷, 类型)
        name_pending: list[tuple[int, str]] = []             # (file_id, 名称文本)

        def store_media(entries: list[tuple[int, Path, Any, str]], vectors) -> None:
            grouped: dict[int, list] = {}
            for (fid, _path, _payload, kind), vec in zip(entries, vectors):
                grouped.setdefault(fid, []).append((_path.name, vec, kind))
            for fid, items in grouped.items():
                self.store.clear_file_chunks(fid, keep_caption=True)
                self.store.add_chunks(fid, items)

        def flush(force_flush: bool = False) -> None:
            if not text_pending and not media_pending and not name_pending:
                return
            if not force_flush and len(text_pending) + len(media_pending) < batch:
                return

            if text_pending:
                # ⚠ 2026-10-19：以前这里是**一次性批量编码**、且**没有异常保护** ✗
                #   批量里只要有一片编不过，整批全废 ✗，异常还会冒到"当时正在处理的文件"上
                #   → 日志里看起来连 .jpg 都报"TextEncodeInput..."✗（张冠李戴）
                #   现在：先整批试；失败就**逐片重试**，把坏的那片单独挑出来 ✓
                entries = list(text_pending)
                text_pending.clear()
                good: list = []
                bad: list = []
                try:
                    vectors = self.engine.encode(
                        [item[1] for item in entries],
                        prompt_name="Document", dim=cfg.index_dim)
                    good = list(zip(entries, vectors))
                except Exception as exc:                          # noqa: BLE001
                    log.info("文本批量编码失败（%s），逐片重试 %d 片", exc, len(entries))
                    for item in entries:
                        try:
                            vec = self.engine.encode([item[1]],
                                                     prompt_name="Document",
                                                     dim=cfg.index_dim)[0]
                            good.append((item, vec))
                        except Exception as sub:                  # noqa: BLE001
                            # ★ 最后一道防线（项目主 2026-10-19：
                            #   "扫描前清洗规则请写入规则，不要以后碰到又报错"）：
                            #   再洗一遍（丢掉代理/私有区/非 BMP 等所有危险字符）重试 ✓
                            #   宁可少几个特殊符号，也别让整片文字进不了库 ✓
                            safe = extract.hard_clean(item[1])
                            if safe and safe != item[1]:
                                try:
                                    vec = self.engine.encode([safe],
                                                             prompt_name="Document",
                                                             dim=cfg.index_dim)[0]
                                    good.append(((item[0], safe, item[2]), vec))
                                    log.info("文本片用「强力清洗」后编码成功"
                                             "（%d 字 → %d 字）", len(item[1]), len(safe))
                                    continue
                                except Exception:                 # noqa: BLE001
                                    pass
                            bad.append((item, sub))
                if good:
                    grouped: dict[int, list] = {}
                    for (fid, text, kind), vec in good:
                        grouped.setdefault(fid, []).append((text, vec, kind))
                    for fid, items in grouped.items():
                        self.store.clear_file_chunks(fid, keep_caption=True)
                        self.store.add_chunks(fid, items)
                    self._update(chunks=self.status()["chunks"] + len(good))
                for (fid, text, _kind), exc3 in bad:
                    # 真正编不过的那一片：按文件记明确原因 ✓（带堆栈，便于定位）
                    note = f"这一片文字编不过（{len(text)} 字）：{type(exc3).__name__}: {exc3}"
                    try:
                        self.store.set_file_error(fid, folder_id, "", note[:300])
                    except Exception:                             # noqa: BLE001
                        pass
                    log.warning("文本片编码失败 file_id=%s len=%s：%s",
                                fid, len(text), exc3, exc_info=True)

            if media_pending:
                entries = list(media_pending)
                media_pending.clear()
                step = max(1, min(8, batch))       # 图片解码占内存，分批小一点
                for start in range(0, len(entries), step):
                    chunk = entries[start:start + step]
                    try:
                        vectors = self.engine.encode([item[2] for item in chunk],
                                                     dim=cfg.index_dim)
                        store_media(chunk, vectors)
                        self._update(chunks=self.status()["chunks"] + len(chunk))
                    except Exception as exc:  # noqa: BLE001
                        # 批量里只要有一个坏文件整批都会失败，所以逐个重试
                        log.info("媒体编码失败（%s），逐个重试 %d 个", exc, len(chunk))
                        good, bad = [], []
                        for entry in chunk:
                            try:
                                vec = self.engine.encode([entry[2]], dim=cfg.index_dim)[0]
                                good.append((entry, vec))
                            except Exception as sub:  # noqa: BLE001
                                bad.append((entry, sub))
                        if good:
                            store_media([e for e, _ in good], [v for _, v in good])
                            self._update(chunks=self.status()["chunks"] + len(good))
                        for (fid, path, _payload, _kind), exc2 in bad:
                            # ⚠ 同上：格式不支持/损坏的媒体**不算异常** ✗（项目主要求 2026-10-18）
                            _m = str(exc2)
                            _soft = any(k in _m for k in (
                                "无法识别", "decoder", "truncated", "cannot identify",
                                "Unsupported", "不支持", "image file is",
                                "cannot be opened", "Unsupported image file",
                            ))
                            if _soft:
                                self.store.mark_ok(
                                    fid, "文件格式不支持或已损坏，已跳过内容（不算异常）")
                            else:
                                self.store.set_file_error(fid, folder_id, path,
                                                          _readable_error(exc2))
                                status = self.status()
                                self._update(errors=status["errors"] + 1)
                            log.warning("跳过无法编码的媒体文件 %s：%s", path.name, exc2)

            if name_pending:
                vectors = self.engine.encode(
                    [item[1] for item in name_pending], prompt_name="Document",
                    dim=cfg.index_dim)
                for (fid, _), vec in zip(name_pending, vectors):
                    self.store.set_name_vec(fid, vec)
                name_pending.clear()

        for path, stat, _sub in work:
            if self._cancel.is_set():
                return
            # 这个文件该用哪套口径 ✓：命中了归类的子目录就用它，否则跟随本层 ✓
            # ⚠ 只有**模态/知识库**会不一样；维度、分块、上限仍取本层（见 folderconf 的取舍）
            fcfg_of = (sub_cfgs or {}).get(_sub) or cfg
            self._update(current=path.name)
            file_id: int | None = None
            try:
                kind = extract.classify(path)
                file_id = self.store.upsert_file(
                    folder_id, path, stat.st_size, stat.st_mtime, kind)

                name_text = f"{path.stem} {path.parent.name} {kind}"
                name_pending.append((file_id, name_text))

                if extract.supports_content(kind, fcfg_of) and stat.st_size <= max_bytes:
                    if kind in ("image", "audio", "video"):
                        # 必须用「带媒体键的字典」，只给路径会被当成纯文本编码
                        media_pending.append((file_id, path, {kind: str(path)}, kind))
                        if kind == "image" and self.cfg.face_enabled:
                            got = self._extract_faces(file_id, path)
                            if got > 0:
                                face_hits = self.status().get("faces", 0) + got
                                self._update(faces=face_hits)
                    else:
                        _kind, raw_text = extract.extract(path, max_bytes)
                        # ★ 默认清洗（项目主 2026-10-18）：去控制字符/零宽字符、
                        #   压空行、丢 URL 编码这类噪声行 → 检索更准 ✓
                        #   清洗后为空就当"没有可提取的文字"✓（不再报异常 ✗）
                        text = extract.clean_text(raw_text)
                        # ★ 「这个 PDF 到底有没有可用正文」不能只看文字空不空 ✗
                        #   2026-10-19 实测：地藏经 160 页**有 515 字**（每页 3.2 字，
                        #   都是扫描页上零星的页码/拼音），老判据会当它是"文字版"
                        #   → 跳过 OCR → 正文全丢 ✗ → 改看**每页字数密度** ✓
                        need_ocr = False
                        if kind == "pdf" and not text.strip():
                            need_ocr = True
                        elif kind == "pdf" and fcfg_of.ocr_enabled:
                            try:
                                from . import ocr as _ocr_mod
                                dens = _ocr_mod.pdf_text_density(path)
                                if dens.get("needs_ocr"):
                                    need_ocr = True
                                    log.info("PDF 文字太稀（%s 字/%s 页，每页 %.1f 字）"
                                             "→ 当作扫描版处理：%s",
                                             dens.get("chars"), dens.get("pages"),
                                             dens.get("per_page", 0), path.name)
                            except Exception as exc:             # noqa: BLE001
                                log.debug("算文字密度失败：%s", exc)
                        if text.strip() and not need_ocr:
                            pieces = extract.chunk_text(
                                text, cfg.chunk_chars, cfg.chunk_overlap,
                                cfg.max_chunks_per_file)
                            for piece in pieces:
                                text_pending.append((file_id, piece, kind))
                            # ★ 插图打标登记（2026-10-19）✓
                            #   项目主选的范围是"1 和 3"：
                            #     · 文字版 PDF → 把筛选后的真插图登记进队列 ✓
                            #     · 扫描版 PDF → 只走整页 OCR 取文字，**不另外标图** ✓
                            #   所以这里挂在"提得出文字"的分支里 ✓
                            if kind == "pdf" and fcfg_of.caption_enabled:
                                self._enqueue_pdf_images(file_id, folder_id, path,
                                                         fcfg_of)
                        else:
                            # ★ 扫描版 OCR（2026-10-19）✓
                            #   项目主要求："放文件进去能自动判断自动 OCR 进知识库" ✓
                            #   判断方式：这个 PDF **提不出可用文字** → 但文件夹开了 OCR
                            #   注意：有文字层的 PDF 根本不会走到这儿（上面就返回了）✓
                            #   所以"自动判断"是天然的：不用另设开关去猜 ✓
                            if kind == "pdf" and fcfg_of.ocr_enabled:
                                ocr_text, ocr_note = self._ocr_pdf_text(path, file_id)
                                ocr_text = extract.clean_text(ocr_text)
                                if ocr_text.strip():
                                    pieces = extract.chunk_text(
                                        ocr_text, cfg.chunk_chars, cfg.chunk_overlap,
                                        cfg.max_chunks_per_file)
                                    for piece in pieces:
                                        text_pending.append((file_id, piece, "pdf"))
                                    self.store.mark_ok(file_id, ocr_note)
                                    log.info("扫描版 OCR 成功：%s（%d 片）",
                                             path.name, len(pieces))
                                else:
                                    self.store.clear_file_chunks(file_id,
                                                                 keep_caption=True)
                                    self.store.mark_ok(
                                        file_id, ocr_note or "扫描版 OCR：没识别出文字")
                            else:
                                self.store.clear_file_chunks(file_id, keep_caption=True)
                                why = ("没有可提取的文字" if not raw_text.strip()
                                       else "清洗后没有有效文字（原文是乱码/链接噪声）")
                                self.store.mark_ok(file_id, why)
                else:
                    self.store.clear_file_chunks(file_id, keep_caption=True)
                    if kind != "image":
                        self.store.clear_file_faces(file_id)
                    if stat.st_size > max_bytes:
                        self.store.mark_ok(file_id, "文件过大，只索引文件名")
                    else:
                        self.store.mark_ok(file_id, "该类型不索引内容")

                status = self.status()
                self._update(done=status["done"] + 1, indexed=status["indexed"] + 1,
                             folder_done=status["folder_done"] + 1)
                flush()
            except Exception as exc:  # noqa: BLE001
                # ⚠ 带堆栈 ✓（2026-10-19：以前只记一句话，看不到真因 ✗；
                #   而 exc_info=True 在这个日志格式下没打出来 ✗ → 直接格式化成文本 ✓）
                import traceback as _tb
                log.warning("索引失败 %s: %s\n%s", path, exc, _tb.format_exc())
                # ⚠ 2026-10-18 项目主要求：**文件本身格式不支持/损坏**不该算"异常"✗
                #   它只是"这个文件没法索引内容"，跟系统出问题不是一回事。
                #   （之前 obsidian 里 27 张 avif/损坏图片全被算成异常，数字吓人 ✓ 现在不算了）
                msg = str(exc)
                soft = any(k in msg for k in (
                    "无法识别", "decoder", "truncated", "cannot identify",
                    "Unsupported", "不支持", "image file is", "cannot be opened",
                ))
                try:
                    if soft and file_id is not None:
                        self.store.mark_ok(
                            file_id, "文件格式不支持或已损坏，已跳过内容（不算异常）")
                    else:
                        self.store.set_file_error(file_id, folder_id, path, msg)
                except Exception:  # noqa: BLE001
                    pass
                status = self.status()
                if soft:
                    # 不计入 errors ✓（但进度照样往前 ✓）
                    self._update(done=status["done"] + 1, indexed=status["indexed"] + 1,
                                 folder_done=status["folder_done"] + 1)
                else:
                    self._update(done=status["done"] + 1, errors=status["errors"] + 1,
                                 folder_done=status["folder_done"] + 1)
        flush(True)
