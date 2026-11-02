"""预览类接口的可见性守卫（多人 P2-b）。

预览面是**最后一个数据口子** ✗：搜索挡住了 ✓、问答挡住了 ✓，但
`/api/preview/{file_id}`、`/api/preview/{file_id}/image|text|pdf-*`、`/api/raw`
本来只要能猜到文件编号或路径就能读到内容 ✗。

为什么用**中间件**而不是给每个端点加参数：
  · 预览面有 6 个 file_id 端点 + 1 个 raw 端点，逐个改容易漏 ✗（已经踩过
    "改端点忘加 request → 500" 的坑 ✓）
  · 中间件在**一处**统一判断 ✓，将来新增预览端点也自动受保护 ✓

判定规则（宁少给不多给）：
  · 管理员 → 放行 ✓
  · `/api/preview/<数字>...` → 查这个 file 属于哪个文件夹 → 不在"可见集合"里就 403 ✓
  · `/api/raw?...path=<绝对路径>` → 若它落在**某个对当前用户不可见的私有文件夹**下 → 403 ✓
    （路径能对上就挡 ✓；对不上的（相对路径等）交回原接口处理，避免把正常预览弄坏 ✗）
  · 任何自身异常 → **放行**（宁可漏挡也不能把预览搞挂 ✗；这是"可用性优先"的取舍，
    已在 docs 里写明 ✓）
"""
from __future__ import annotations

import logging
import re
import sqlite3
import urllib.parse

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import COOKIE_NAME, Auth

log = logging.getLogger("eg2.previewguard")

_ID_RE = re.compile(r"^/api/preview/(\d+)(?:/|$)")


def _q(con: sqlite3.Connection, sql: str, args: tuple):
    try:
        return con.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None


class PreviewGuard(BaseHTTPMiddleware):
    """预览/原图接口按"文件所属文件夹是否可见"放行。"""

    def __init__(self, app, db_path) -> None:                    # noqa: ANN001
        super().__init__(app)
        self.db_path = db_path
        self.auth = Auth(db_path)

    # ---------- 内部 ----------
    def _user(self, request):                                    # noqa: ANN001
        try:
            return self.auth.who(request.cookies.get(COOKIE_NAME))
        except Exception:                                        # noqa: BLE001
            return None

    def _private_roots(self, user) -> list[tuple[int, str]]:     # noqa: ANN001
        """当前用户**看不到**的私有文件夹（id, path），用于 /api/raw 的前缀判断。"""
        try:
            from .multiuser import visible_folder_ids

            vis = visible_folder_ids(self.db_path, user)
            if vis is None:
                return []                                        # 管理员：没有要挡的 ✓
            keep = {int(x) for x in vis}
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
            try:
                rows = con.execute(
                    "SELECT id, path FROM folders WHERE visibility = 'private'").fetchall()
            finally:
                con.close()
            return [(int(r[0]), str(r[1])) for r in rows if int(r[0]) not in keep]
        except Exception:                                        # noqa: BLE001
            return []

    def _file_folder(self, file_id: int):
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        try:
            row = _q(con, "SELECT folder_id FROM files WHERE id=?", (int(file_id),))
            return None if row is None else int(row[0] or 0)
        finally:
            con.close()

    def _is_named_cover(self, face_id: int) -> bool:
        """这张脸是不是**某个已命名人物**的封面脸（人物头像 = 共享元数据 ✓）。

        2026-10-17 项目主要求：用户端要**实时显示管理员设置的头像与名称** ✓ ——
        而管理员选的那张封面脸可能来自他的私有文件夹，按原规则会被 403 挡掉 ✗
        （用户端就成了空白头像，跟管理员那边不一样）。
        所以只对"**已命名人物的封面**"放行 ✓：它展示的是"这个人长什么样"，
        不是"某张私有照片" ✓。未命名人物的脸、以及人物的其它照片**照旧挡住** ✗。
        """
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
            try:
                row = _q(con, "SELECT 1 FROM people WHERE cover_face = ? "
                              "AND TRIM(COALESCE(name, '')) <> ''", (int(face_id),))
            finally:
                con.close()
            return row is not None
        except Exception:                                        # noqa: BLE001
            return False

    async def dispatch(self, request, call_next):                 # noqa: ANN001
        try:
            path = request.url.path
            if path.startswith("/api/preview/") or path == "/api/raw":
                user = self._user(request)
                if not (user and user.get("role") == "admin"):
                    from .multiuser import visible_folder_ids

                    vis = visible_folder_ids(self.db_path, user)
                    if vis is not None:                          # None = 管理员不限制
                        keep = {int(x) for x in vis}
                        m = _ID_RE.match(path)
                        if m:
                            fid = self._file_folder(int(m.group(1)))
                            if fid is not None and fid not in keep:
                                return JSONResponse(status_code=403, content={
                                    "detail": "这个文件所在的文件夹对你是私有的，看不到内容"})
                        elif path == "/api/raw":
                            raw = urllib.parse.parse_qs(
                                request.url.query).get("path", [""])[0]
                            if raw and (":" in raw or raw.startswith("/")
                                        or raw.startswith("\\\\")):
                                low = raw.replace("/", "\\").lower()
                                for _fid, root in self._private_roots(user):
                                    r = root.replace("/", "\\").lower().rstrip("\\")
                                    if r and (low == r or low.startswith(r + "\\")):
                                        return JSONResponse(status_code=403, content={
                                            "detail": "这个路径在私有文件夹里，看不到内容"})
        except Exception as exc:                                 # noqa: BLE001
            # 可用性优先：守卫本身出错就放行，绝不把正常预览弄挂 ✗
            log.warning("预览守卫异常，放行本次请求：%s", exc)
        # 人脸缩略图（/api/faces/<id>/thumb）：它不走 /api/preview ✗，
        # 所以单独判一次 —— 这张脸所属的文件在哪个文件夹，那个文件夹可见才行 ✓
        # （私有照片里的人物头像，不该被外人取到 ✓）
        try:
            _fm = re.match(r"^/api/faces/(\d+)/thumb(?:/|$)", path)
            if _fm:
                _u = self._user(request)
                if not (_u and _u.get("role") == "admin"):
                    from .multiuser import visible_folder_ids as _vf

                    _vis = _vf(self.db_path, _u)
                    if _vis is not None:
                        _con = sqlite3.connect(
                            f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
                        try:
                            _row = _q(_con, "SELECT f.folder_id FROM faces fa "
                                            "JOIN files f ON f.id = fa.file_id "
                                            "WHERE fa.id = ?", (int(_fm.group(1)),))
                        finally:
                            _con.close()
                        if _row is not None and int(_row[0] or 0) not in {
                                int(x) for x in _vis}:
                            # 例外：已命名人物的**封面脸**放行 ✓（头像要跟管理员一致 ✓）
                            if not self._is_named_cover(int(_fm.group(1))):
                                return JSONResponse(status_code=403, content={
                                    "detail": "这张人脸所在的文件夹对你是私有的，看不到"})
        except Exception as _exc:                                # noqa: BLE001
            log.warning("人脸缩略图守卫异常，放行：%s", _exc)

        return await call_next(request)
