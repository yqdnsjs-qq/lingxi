"""管理界面接口：文件夹的「共享 / 私有」（多人 P3）。

规则（见 docs/多人使用设计.md）：
  · `shared`  = 共享库：**所有登录用户**都能看（没开强制登录时，匿名也能看 ✓）
  · `private` = 私有：只有 **owner** 能看；owner_id=0（管理员建的库）时 = **只有管理员**能看 ✓
  · 只有管理员能改这里 ✓（普通用户改 → 403 ✓，并且要带 CSRF 头 ✓）

直接读库（不走 Store），避免依赖它的内部 API —— 这里只做"列文件夹 + 改一个字段"。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request

from .auth import COOKIE_NAME, CSRF_HEADER, Auth
from .config import Config
from .multiuser import VISIBILITIES, set_visibility

log = logging.getLogger("eg2.admin")


def _rows(db: str | Path, sql: str, args: tuple = ()) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def create_router(cfg: Config) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])
    auth = Auth(cfg.db_path)

    def _need_admin(request: Request) -> dict:
        u = auth.who(request.cookies.get(COOKIE_NAME))
        if not u:
            raise HTTPException(status_code=401, detail="请先登录")
        if u.get("role") != "admin":
            raise HTTPException(status_code=403, detail="只有管理员能改共享设置")
        tok = (request.headers.get(CSRF_HEADER) or "").strip()
        if not tok or tok != (u.get("csrf") or ""):
            raise HTTPException(status_code=403, detail="缺少或错误的 CSRF 头")
        return u

    @router.get("/folders")
    async def list_folders(request: Request):
        """列文件夹 + 归属 + 共享状态（管理页用）。管理员才能看。"""
        u = auth.who(request.cookies.get(COOKIE_NAME))
        if not u or u.get("role") != "admin":
            raise HTTPException(status_code=403, detail="只有管理员能看这个列表")
        cols = {r[1] for r in sqlite3.connect(cfg.db_path).execute(
            "PRAGMA table_info(folders)")}
        has_owner = "owner_id" in cols
        has_vis = "visibility" in cols
        sel = ("id, path, status, "
               + ("owner_id, " if has_owner else "0 AS owner_id, ")
               + ("visibility, " if has_vis else "'shared' AS visibility, ")
               + ("index_state" if "index_state" in cols else "'ready' AS index_state"))
        folders = _rows(cfg.db_path, f"SELECT {sel} FROM folders ORDER BY id")
        counts = {int(r["folder_id"]): int(r["n"]) for r in _rows(
            cfg.db_path, "SELECT folder_id, COUNT(*) AS n FROM files GROUP BY folder_id")}
        users = {int(r["id"]): str(r["name"]) for r in _rows(
            cfg.db_path, "SELECT id, name FROM users")} if "users" in {
            r[0] for r in sqlite3.connect(cfg.db_path).execute(
                "SELECT name FROM sqlite_master WHERE type='table'")} else {}
        for f in folders:
            f["files"] = counts.get(int(f["id"]), 0)
            f["owner_name"] = users.get(int(f["owner_id"] or 0), "")
            f["readable_by"] = ("所有人 ✓" if f["visibility"] == "shared"
                                else ("只有管理员 ✓" if not f["owner_id"]
                                      else f"只有 {f['owner_name'] or ('用户' + str(f['owner_id']))} ✓"))
        return {"ok": True, "folders": folders, "can_edit": True,
                "note": "共享 = 所有登录用户可读；私有 = 只有归属人（owner_id=0 时=只有管理员）可读"}

    @router.post("/folders/{folder_id}/visibility")
    async def set_folder_visibility(folder_id: int, request: Request,
                                    payload: dict = Body(default={})):
        _need_admin(request)
        vis = str((payload or {}).get("visibility") or "").strip()
        if vis not in VISIBILITIES:
            raise HTTPException(status_code=400,
                                detail="visibility 只能是 shared 或 private")
        got = _rows(cfg.db_path, "SELECT id, path FROM folders WHERE id=?", (folder_id,))
        if not got:
            raise HTTPException(status_code=404, detail="没有这个文件夹")
        set_visibility(cfg.db_path, folder_id, vis)
        log.info("文件夹 %s 的可见性改为 %s（by 管理员）", folder_id, vis)
        return {"ok": True, "folder_id": folder_id, "visibility": vis,
                "path": got[0]["path"],
                "note": "已改为共享" if vis == "shared" else "已改为私有（只有归属人可见）"}

    return router
