"""PDF 内嵌插图的提取与筛选（给"插图打标 + 定位"用）。

为什么要单独一个模块：**图太多**。
  实测：花生壳手册前 10 页有 440 个图片对象 ✗
  —— 里面有大量图标、装饰线、水印、重复引用；
  全打标的话，一次扫描就是几百次视觉模型调用，既慢又费钱。

所以先**筛选**（尺寸/面积/重复），只把"真正的插图"挑出来打标 ✓
打标本身复用现有的 caption 链路（外部视觉模型）✓
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger("eg2.pdfimg")

# 筛选阈值（都能调）
MIN_PIXELS = 120          # 最短边小于这个像素 → 当图标丢掉
MIN_AREA_RATIO = 0.02     # 占页面面积小于 2% → 当装饰丢掉
MAX_PER_PDF = 30          # 每个 PDF 最多挑这么多张去标（防爆）
MAX_BYTES = 12 * 1024 * 1024   # 单张图超过这个大小就不标（模型吃不下）


def list_images(path: Path, limit: int = MAX_PER_PDF) -> list[dict]:
    """列出 PDF 里**值得打标**的插图。

    返回：[{page, xref, width, height, area_ratio, bytes, sha1, ext, data}]
      · page     —— 第几页（从 1 开始 ✓ 界面上要给用户看）
      · xref     —— PDF 内部对象号
      · area_ratio —— 占页面面积比例（判断是不是主图）
      · sha1     —— 内容哈希（跨页/跨文件去重用 ✓）
      · data     —— 原始图片字节（调用方拿去打标）

    筛选规则（宁可少标，不要标一堆图标）：
      1. 尺寸：宽高都要 ≥ MIN_PIXELS
      2. 面积：占页面 ≥ MIN_AREA_RATIO
      3. 去重：同一份内容（sha1）只留一份
      4. 数量：最多 limit 张
    """
    out: list[dict] = []
    seen_sha: set[str] = set()
    try:
        import fitz
    except ImportError:
        log.debug("没装 PyMuPDF，跳过插图提取")
        return out

    try:
        doc = fitz.open(str(path))
    except Exception as exc:                                     # noqa: BLE001
        log.debug("打开 PDF 失败（%s）：%s", path, exc)
        return out

    try:
        for pno in range(len(doc)):
            if len(out) >= limit:
                break
            page = doc[pno]
            try:
                page_area = float(page.rect.width * page.rect.height) or 1.0
                infos = page.get_images(full=True) or []
            except Exception:                                    # noqa: BLE001
                continue
            for info in infos:
                if len(out) >= limit:
                    break
                try:
                    xref = int(info[0])
                    width = int(info[2])
                    height = int(info[3])
                except Exception:                                # noqa: BLE001
                    continue
                if min(width, height) < MIN_PIXELS:
                    continue
                # 位置（用来算占页面多大；拿不到就跳过面积判断）
                ratio = 0.0
                try:
                    rects = page.get_image_rects(xref) or []
                    if rects:
                        r = rects[0]
                        ratio = float(r.width * r.height) / page_area
                except Exception:                                # noqa: BLE001
                    rects = []
                if rects and ratio < MIN_AREA_RATIO:
                    continue
                try:
                    raw = doc.extract_image(xref) or {}
                    data = raw.get("image") or b""
                    ext = str(raw.get("ext") or "png")
                except Exception:                                # noqa: BLE001
                    continue
                if not data or len(data) > MAX_BYTES:
                    continue
                sha = hashlib.sha1(data).hexdigest()
                if sha in seen_sha:                              # 重复引用同一张图 ✓
                    continue
                seen_sha.add(sha)
                out.append({
                    "page": pno + 1, "xref": xref,
                    "width": width, "height": height,
                    "area_ratio": round(ratio, 4),
                    "bytes": len(data), "sha1": sha, "ext": ext,
                    "data": data,
                })
    finally:
        doc.close()
    return out


def images_report(path: Path) -> dict:
    """只统计不提取（给"计划"用：先估算这个 PDF 有几张要标的图）✓"""
    imgs = list_images(path, limit=999)
    return {"total_raw": len(imgs), "pages": sorted({i["page"] for i in imgs}),
            "sizes": [(i["width"], i["height"]) for i in imgs[:10]]}
