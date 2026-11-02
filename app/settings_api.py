"""界面设置的 HTTP 接口：读取、切换模式、重载模型。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Request

from . import settings as S
from .config import Config
from .indexer import Indexer
from .model import EmbeddingEngine
from .store import Store

log = logging.getLogger("eg2.settings")


def create_router(cfg: Config, store: Store, engine: EmbeddingEngine,
                  indexer: Indexer) -> APIRouter:
    router = APIRouter()

    # 换库时把这里这份 store 也换掉 —— 之前漏了它，结果换库之后
    # 「设置」页读到的是已经关闭的旧连接，报 500
    # （sqlite3.ProgrammingError: Cannot operate on a closed database）。
    from .dbreload import register as _register_reload

    def _rebind_db(new_store) -> None:
        nonlocal store
        store = new_store

    _register_reload("settings-router", _rebind_db)

    def snapshot() -> dict:
        return {
            "ok": True,
            "schema": S.SCHEMA,
            "presets": S.PRESETS,
            "values": S.current_values(cfg),
            "active_preset": S.active_preset(cfg),
            "model": {**engine.reload_state(), "device": engine.device,
                      "dtype": engine.dtype_name, "loaded": engine.model is not None},
            "index": {"running": indexer.is_running(), **store.stats()},
        }

    # ---- 写路径权限（多人 P2-b ②）：全局设置只有管理员能动 ----
    # 规则和 app/filesearch.py 里那套一致（设计文档 §1）；users 表为空的老库
    # 一律放行 ✓，避免单人模式被自己的权限检查锁死。
    def _need_admin(request) -> dict | None:                      # noqa: ANN001
        try:
            import sqlite3 as _sq

            con = _sq.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=10)
            try:
                if not con.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                    return None                               # 没启用账号体系 → 放行 ✓
            finally:
                con.close()
        except Exception:                                         # noqa: BLE001
            return None
        from .auth import COOKIE_NAME, Auth

        u = Auth(cfg.db_path).who(request.cookies.get(COOKIE_NAME))
        if not u:
            raise HTTPException(status_code=401, detail="请先登录（这个操作只有管理员能做）")
        if u.get("role") != "admin":
            raise HTTPException(status_code=403, detail="这个操作只有管理员能做")
        return u

    @router.get("/api/settings")
    async def get_settings():
        return snapshot()

    @router.post("/api/settings")
    async def put_settings(request: Request, payload: dict = Body(...)):
        """改全局设置（设备/模态/端口/模型…）—— 影响所有人 → 只有管理员 ✓。"""
        _need_admin(request)
        preset_id = payload.get("preset")
        updates = dict(payload.get("values") or {})

        if preset_id:
            preset = next((p for p in S.PRESETS if p["id"] == preset_id), None)
            if preset is None:
                raise HTTPException(status_code=400, detail=f"未知模式：{preset_id}")
            updates = {**preset["values"], **updates}
            log.info("切换到运行模式：%s", preset["name"])

        if not updates:
            raise HTTPException(status_code=400, detail="没有需要修改的设置项")

        before_dim = store.get_meta("dim")
        result = S.apply(cfg, updates)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail="；".join(result.get("errors", [])))

        after_dim = store.check_dim()
        if str(after_dim) != str(before_dim):
            store.set_meta("dim", str(after_dim))
        if result.get("need_reload"):
            result["message"] = "设置已保存；需要重新加载模型才能生效。"
        else:
            result["message"] = "设置已保存，立即生效。"

        result.update({"presets": S.PRESETS, "schema": S.SCHEMA,
                       "model": engine.reload_state(), "index": store.stats()})
        return result

    @router.post("/api/model/reload")
    async def reload_model(request: Request):
        """重新加载嵌入模型（全局动作）→ 只有管理员 ✓"""
        _need_admin(request)
        started = engine.reload_async()
        return {"started": started, "message": "正在重新加载模型…" if started else "已经在重载了",
                **engine.reload_state()}

    @router.get("/api/model/status")
    async def model_status():
        return {**engine.reload_state(), "device": engine.device, "dtype": engine.dtype_name,
                "loaded": engine.model is not None,
                "modality": cfg.modality, "dimension": engine.dim}

    return router
